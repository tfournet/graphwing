#!/usr/bin/env python3
"""Publish catalog graphs in org tim-graphwing. Substitutes $GRAPHWING_INSTANCE."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPHS = ROOT / "graphs"
INSTALL_PATHS = [
    ROOT / "rewst-install.json",
    Path.home() / ".graphwing" / "rewst-install.json",
]
TENANT = "485bb4f1-4f83-4f81-8079-618e1139e22b"
BASE = "https://app.rewst.ai/api"
SOURCE = "https://graphwing.tfour.net/openapi.json"


def load_install() -> dict:
    for p in INSTALL_PATHS:
        if p.is_file():
            return json.loads(p.read_text())
    raise SystemExit("rewst-install.json missing")


def bws_mcp() -> str:
    token = None
    for p in (
        Path.home() / ".hermes/.env",
        Path.home() / ".hermes/profiles/executor/.env",
        Path.home() / ".hermes/profiles/executor-max/.env",
        Path.home() / ".hermes/profiles/riftwing-planner/.env",
    ):
        if not p.is_file():
            continue
        for line in p.read_text().splitlines():
            if line.startswith("BWS_ACCESS_TOKEN=") and len(line) > 20:
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
        if token:
            break
    if not token:
        raise SystemExit("no BWS_ACCESS_TOKEN")
    env = {**os.environ, "BWS_ACCESS_TOKEN": token}
    secrets = json.loads(subprocess.check_output(["bws", "secret", "list", "-o", "json"], env=env, text=True))
    sid = next(s["id"] for s in secrets if s.get("key") == "app_rewst_ai__tim_graphwing_mcp_token")
    got = json.loads(subprocess.check_output(["bws", "secret", "get", sid, "-o", "json"], env=env, text=True))
    mcp = got["value"]
    payload = mcp.split(".")[1]
    pad = "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload + pad))
    tid = claims.get("tenant_id") or claims.get("tid")
    if tid != TENANT:
        raise SystemExit(f"jwt tenant {tid} != {TENANT}")
    return mcp


def api(mcp: str, method: str, path: str, body=None, timeout=120):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {mcp}",
            "X-Tenant-ID": TENANT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw[:1200]}
        return e.code, parsed


def must(st, body, ok=(200, 201, 202), label=""):
    if st not in ok:
        raise SystemExit(f"{label} HTTP {st}: {json.dumps(body)[:800]}")
    return body


def subst_instance(obj, instance: str):
    if isinstance(obj, str):
        return obj.replace("$GRAPHWING_INSTANCE", instance)
    if isinstance(obj, list):
        return [subst_instance(x, instance) for x in obj]
    if isinstance(obj, dict):
        return {k: subst_instance(v, instance) for k, v in obj.items()}
    return obj


def load_graph(name: str, instance: str) -> dict:
    raw = json.loads((GRAPHS / f"{name}.json").read_text())
    return subst_instance(raw, instance)


def upsert_workflow(mcp: str, name: str, slug: str, description: str, spec: dict):
    st, by_slug = api(mcp, "GET", f"/workflows/{slug}")
    existing = by_slug if st == 200 and by_slug.get("id") else None
    if not existing:
        st, listing = api(mcp, "GET", "/workflows?limit=100")
        items = listing.get("workflows") or listing.get("items") or listing.get("data") or []
        for w in items if isinstance(items, list) else []:
            if w.get("slug") == slug or w.get("name") == name:
                existing = w
                break
    if existing:
        wid = existing["id"]
        print(f"update {slug} {wid}")
        st, wf = api(mcp, "GET", f"/workflows/{wid}?include=spec")
        must(st, wf, label=f"get {slug}")
        vid = (wf.get("currentVersion") or {}).get("id")
        if (wf.get("currentVersion") or {}).get("status") == "published":
            st, rec = api(mcp, "POST", f"/workflows/{wid}/versions", {})
            must(st, rec, ok=(200, 201), label="create draft version")
            vid = (rec.get("currentVersion") or {}).get("id")
            print("new draft", vid)
        st, patched = api(mcp, "PATCH", f"/workflows/{wid}/versions/{vid}", {"spec": spec})
        print("patch", st)
        if st not in (200, 201):
            raise SystemExit(f"patch {st} {json.dumps(patched)[:800]}")
        warnings = patched.get("validationWarnings") or []
        errors = [w for w in warnings if (w.get("severity") or "").lower() == "error"]
        if errors:
            raise SystemExit(f"validation errors {errors}")
        if warnings:
            print("warnings", json.dumps(warnings)[:800])
        st, pub = api(mcp, "POST", f"/workflows/{wid}/versions/{vid}/publish", {})
        if st not in (200, 201, 202):
            raise SystemExit(f"publish failed {st} {json.dumps(pub)[:500]}")
        print("published", slug, wid, vid)
        return wid, vid, slug
    print(f"create {slug}")
    st, created = api(mcp, "POST", "/workflows", {"name": name, "description": description})
    must(st, created, label="create workflow")
    wid = created["id"]
    vid = (created.get("currentVersion") or created.get("current_version") or {}).get("id")
    if not vid:
        raise SystemExit(f"no version id {created}")
    st, patched = api(mcp, "PATCH", f"/workflows/{wid}/versions/{vid}", {"spec": spec})
    if st not in (200, 201):
        raise SystemExit(f"patch spec {st} {json.dumps(patched)[:800]}")
    warnings = patched.get("validationWarnings") or []
    errors = [w for w in warnings if (w.get("severity") or w.get("level") or "").lower() == "error"]
    if errors:
        raise SystemExit(f"validation errors {errors}")
    if warnings:
        print("warnings", json.dumps(warnings)[:800])
    st, pub = api(mcp, "POST", f"/workflows/{wid}/versions/{vid}/publish", {})
    must(st, pub, label="publish workflow")
    print("published", slug, wid, vid)
    return wid, vid, created.get("slug") or slug


def run_slug(mcp: str, slug: str, payload: dict, wait: int = 90):
    print(f"=== run {slug} ===")
    st, body = api(mcp, "POST", f"/workflows/{slug}/run", payload)
    must(st, body, ok=(200, 201, 202), label=f"run {slug}")
    rid = body.get("id")
    print("run", rid, "status", body.get("status"))
    deadline = time.time() + wait
    i = 0
    while time.time() < deadline:
        st, r = api(mcp, "GET", f"/runs/{rid}")
        rs = r.get("status")
        print(f"  t={i} {rs}")
        if rs in ("completed", "failed", "cancelled", "timed_out", "timedOut"):
            st, trace = api(mcp, "GET", f"/runs/{rid}/trace")
            Path(f"/tmp/gw-run-{slug}.json").write_text(json.dumps({"run": r, "trace": trace}, indent=2))
            return rs, rid, r, trace
        time.sleep(3)
        i += 1
    raise SystemExit(f"{slug} timeout")


def save_install(install: dict) -> None:
    text = json.dumps(install, indent=2) + "\n"
    for p in INSTALL_PATHS:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="pr-drive", help="graph file stem, or 'all'")
    ap.add_argument("--no-run", action="store_true")
    args = ap.parse_args()
    install = load_install()
    instance = install["instance_id"]
    integration = install["custom_integration_id"]
    mcp = bws_mcp()
    stems = ["verify-stack", "implement-slice", "pr-drive"] if args.only == "all" else [args.only]
    published = {}
    for stem in stems:
        g = load_graph(stem, instance)
        wid, vid, slug = upsert_workflow(mcp, g["name"], g["slug"], g["description"], g["spec"])
        published[stem] = {"workflow_id": wid, "workflow_version_id": vid, "slug": slug}
    if not args.no_run and "pr-drive" in published:
        rs, rid, _, _ = run_slug(
            mcp,
            "graphwing-pr-drive",
            {"input": {"repo": "riftwing"}},
            wait=60,
        )
        published["pr-drive"]["run_id"] = rid
        published["pr-drive"]["status"] = rs
        published["pr-drive"]["smoke"] = "missing_pr"
    install["custom_integration_id"] = integration
    install["pr_drive"] = published.get("pr-drive") or install.get("pr_drive")
    if "verify-stack" in published:
        install["verify_stack"] = {**(install.get("verify_stack") or {}), **published["verify-stack"]}
    if "implement-slice" in published:
        install["implement_slice"] = {**(install.get("implement_slice") or {}), **published["implement-slice"]}
    save_install(install)
    print("=== DONE ===")
    print(json.dumps(published, indent=2))


if __name__ == "__main__":
    main()
