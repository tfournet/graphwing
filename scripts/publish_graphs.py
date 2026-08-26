#!/usr/bin/env python3
"""Publish catalog graphs to a Rewst org. Substitutes instance and hook placeholders.

Seat config is $GRAPHWING_HOME/rewst-install.json (see examples/rewst-install.example.json).
Public OpenAPI URL comes from named-tunnel meta or GRAPHWING_PUBLIC_URL.
MCP token: GRAPHWING_REWST_MCP_TOKEN, or GRAPHWING_REWST_MCP_BWS_KEY + BWS_ACCESS_TOKEN.
"""

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
HOME = Path(os.environ.get("GRAPHWING_HOME", Path.home() / ".graphwing"))
BASE = os.environ.get("GRAPHWING_REWST_API", "https://app.rewst.ai/api").rstrip("/")
TENANT = ""


_VOLATILE_READBACK_FIELDS = frozenset({
    "createdAt", "updatedAt", "created_at", "updated_at", "publishedAt",
    "published_at", "lastPublishedAt", "last_published_at", "etag",
    "revision", "versionId", "version_id", "lastModified", "last_modified",
})


def normalize_catalog(value):
    """Remove only known API metadata, preserving every catalog semantic key."""
    if isinstance(value, dict):
        return {
            key: normalize_catalog(item) for key, item in value.items()
            if key not in _VOLATILE_READBACK_FIELDS
        }
    if isinstance(value, list):
        return [normalize_catalog(item) for item in value]
    return value


def canonical_catalog_bytes(value) -> bytes:
    """The committed catalog parity contract: recursively sorted JSON bytes.

    Hashing a request body proves only what was submitted.  This canonical form
    is applied independently to the catalog object and a fresh API readback,
    and never persisted as a snapshot.
    """
    return json.dumps(normalize_catalog(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def catalog_hash(value) -> str:
    import hashlib
    return hashlib.sha256(canonical_catalog_bytes(value)).hexdigest()


def normalize_workflow_spec(value):
    """Remove only Rewst-owned fields generated for trigger nodes on publish."""
    normalized = normalize_catalog(value)
    if not isinstance(normalized, dict):
        return normalized
    nodes = normalized.get("nodes")
    if not isinstance(nodes, list):
        return normalized
    for node in nodes:
        if not isinstance(node, dict) or not str(node.get("type") or "").startswith("trigger."):
            continue
        config = node.get("config")
        if isinstance(config, dict):
            config.pop("triggerId", None)
            config.pop("triggerUrl", None)
    return normalized


def deployed_openapi_spec() -> dict:
    """Read the installed catalog copy; repository loopback defaults are not authority."""
    path = HOME / "openapi.json"
    try:
        spec = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"deployed OpenAPI unreadable at {path}: {exc}") from exc
    if not isinstance(spec, dict):
        raise SystemExit(f"deployed OpenAPI unreadable at {path}")
    return spec


def fetch_public_openapi(url: str) -> dict:
    """Fetch the exact sourceUrl before a Rewst mutation, without caching it."""
    try:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "graphwing-release-verifier/1",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
        spec = json.loads(raw)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise SystemExit(f"public OpenAPI unreadable at {url}: {exc}") from exc
    if not isinstance(spec, dict):
        raise SystemExit(f"public OpenAPI unreadable at {url}")
    return spec


def require_deployed_public_parity(deployed: dict, public: dict) -> dict:
    """Require semantic parity while retaining server destination as a semantic key."""
    if not isinstance(deployed, dict) or not isinstance(public, dict):
        raise SystemExit("deployed/public OpenAPI unreadable; no Rewst mutation")
    deployed_sha, public_sha = catalog_hash(deployed), catalog_hash(public)
    if deployed_sha != public_sha:
        raise SystemExit(f"deployed/public OpenAPI mismatch; no Rewst mutation ({deployed_sha} != {public_sha})")
    return {"deployed_catalog_sha": deployed_sha, "fresh_public_source_sha": public_sha}


def require_catalog_parity(expected, live, label: str) -> dict:
    """Fail closed on unreadable or normalized catalog/readback mismatch."""
    if not isinstance(live, (dict, list)):
        raise SystemExit(f"{label} live readback is unreadable; no parity receipt")
    expected_sha, live_sha = catalog_hash(expected), catalog_hash(live)
    if expected_sha != live_sha:
        raise SystemExit(f"{label} live catalog mismatch; no parity receipt ({expected_sha} != {live_sha})")
    return {
        "live_source": "fresh_post_publish_api_read",
        "normalized_catalog_sha": expected_sha,
        "normalized_live_sha": live_sha,
    }


def workflow_readback_spec(body: dict) -> dict | None:
    """Extract a workflow spec only from the post-publish API response."""
    if not isinstance(body, dict):
        return None
    envelope = body.get("data") if isinstance(body.get("data"), dict) else body
    workflow = envelope.get("workflow") if isinstance(envelope.get("workflow"), dict) else envelope
    current = workflow.get("currentVersion") or workflow.get("current_version")
    if isinstance(current, dict) and isinstance(current.get("spec"), dict):
        return current["spec"]
    return workflow.get("spec") if isinstance(workflow.get("spec"), dict) else None


def verify_workflow_parity(mcp: str, workflow_id: str, expected_spec: dict, slug: str) -> dict:
    status, body = api(mcp, "GET", f"/workflows/{workflow_id}?include=spec")
    if status != 200:
        raise SystemExit(f"{slug} live readback HTTP {status}; no parity receipt")
    return require_catalog_parity(
        normalize_workflow_spec(expected_spec),
        normalize_workflow_spec(workflow_readback_spec(body)),
        slug,
    )


def load_install() -> dict:
    path = HOME / "rewst-install.json"
    if not path.is_file():
        raise SystemExit(f"missing {path} (copy examples/rewst-install.example.json)")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must be a JSON object")
    return data


def tenant_id(install: dict) -> str:
    tid = (os.environ.get("GRAPHWING_REWST_ORG_ID") or install.get("org_id") or "").strip()
    if not tid:
        raise SystemExit("set org_id in rewst-install.json or GRAPHWING_REWST_ORG_ID")
    return tid


def public_openapi_url() -> str:
    env = os.environ.get("GRAPHWING_PUBLIC_URL", "").strip().rstrip("/")
    if env:
        return env if env.endswith("/openapi.json") else env + "/openapi.json"
    meta = HOME / "cloudflared-meta.json"
    if meta.is_file():
        host = str(json.loads(meta.read_text()).get("hostname") or "").strip().rstrip("/")
        if host:
            if host.startswith("http://") or host.startswith("https://"):
                return host.rstrip("/") + "/openapi.json"
            return f"https://{host}/openapi.json"
    spec_path = HOME / "openapi.json"
    if spec_path.is_file():
        spec = json.loads(spec_path.read_text())
        servers = spec.get("servers") or []
        if servers and isinstance(servers[0], dict) and servers[0].get("url"):
            base = str(servers[0]["url"]).rstrip("/")
            if "127.0.0.1" in base or "localhost" in base:
                raise SystemExit("OpenAPI servers.url is loopback; run named tunnel setup or set GRAPHWING_PUBLIC_URL")
            return base + "/openapi.json"
    raise SystemExit("no public OpenAPI URL: named tunnel setup writes cloudflared-meta.json, or set GRAPHWING_PUBLIC_URL")


def bws_secret(key: str) -> str:
    token = os.environ.get("BWS_ACCESS_TOKEN", "").strip()
    if not token:
        raise SystemExit("BWS_ACCESS_TOKEN required for GRAPHWING_REWST_MCP_BWS_KEY")
    env = {**os.environ, "BWS_ACCESS_TOKEN": token}
    secrets = json.loads(subprocess.check_output(["bws", "secret", "list", "-o", "json"], env=env, text=True))
    sid = next((s["id"] for s in secrets if s.get("key") == key), None)
    if not sid:
        raise SystemExit(f"BWS secret not found: {key}")
    got = json.loads(subprocess.check_output(["bws", "secret", "get", sid, "-o", "json"], env=env, text=True))
    val = got.get("value") or ""
    if not val:
        raise SystemExit(f"BWS secret {key} empty")
    return val


def rewst_mcp(install: dict) -> str:
    direct = os.environ.get("GRAPHWING_REWST_MCP_TOKEN", "").strip()
    if direct:
        mcp = direct
    else:
        bws_key = (os.environ.get("GRAPHWING_REWST_MCP_BWS_KEY") or install.get("mcp_bws_key") or "").strip()
        if not bws_key:
            raise SystemExit("set GRAPHWING_REWST_MCP_TOKEN or GRAPHWING_REWST_MCP_BWS_KEY")
        mcp = bws_secret(bws_key)
    payload = mcp.split(".")[1]
    pad = "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload + pad))
    tid = claims.get("tenant_id") or claims.get("tid")
    expected = tenant_id(install)
    if tid and tid != expected:
        raise SystemExit(f"jwt tenant {tid} != {expected}")
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


def subst_instance(
    obj,
    instance: str,
    hook_secret: str = "",
    status_repo: str = "",
    source_workflow_id: str = "",
):
    if isinstance(obj, str):
        return (
            obj.replace("$GRAPHWING_INSTANCE", instance)
            .replace("$GRAPHWING_HOOK_SECRET", hook_secret)
            .replace("$GRAPHWING_STATUS_REPO", status_repo)
            .replace("$GRAPHWING_SOURCE_WORKFLOW_ID", source_workflow_id)
        )
    if isinstance(obj, list):
        return [subst_instance(x, instance, hook_secret, status_repo, source_workflow_id) for x in obj]
    if isinstance(obj, dict):
        return {
            k: subst_instance(v, instance, hook_secret, status_repo, source_workflow_id)
            for k, v in obj.items()
        }
    return obj


def load_graph(
    name: str,
    instance: str,
    hook_secret: str = "",
    status_repo: str = "",
    source_workflow_id: str = "",
) -> dict:
    raw = json.loads((GRAPHS / f"{name}.json").read_text())
    return subst_instance(raw, instance, hook_secret, status_repo, source_workflow_id)


def resolve_stems(only: str) -> list[str]:
    return (
        [
            "verify-stack",
            "implement-slice",
            "visual-evidence",
            "visual-iteration",
            "pr-drive",
            "pr-status",
            "pre-pr-build",
            "build-completion-supervisor",
            "pr-lifecycle",
        ]
        if only == "all"
        else [only]
    )


def installed_source_workflow_id(install: dict) -> str:
    source = install.get("pre_pr_build")
    workflow_id = source.get("workflow_id") if isinstance(source, dict) else None
    return str(workflow_id or "").strip()


def publish_selected(
    mcp: str,
    install: dict,
    stems: list[str],
    instance: str,
    hook_secret: str,
    status_repo: str,
) -> dict:
    published: dict[str, dict] = {}
    source_workflow_id = installed_source_workflow_id(install)
    for stem in stems:
        if stem == "build-completion-supervisor" and not source_workflow_id:
            raise SystemExit(
                "publishing build-completion-supervisor requires pre-pr-build workflow_id; "
                "publish pre-pr-build first or use --only all"
            )
        g = load_graph(
            stem,
            instance,
            hook_secret,
            status_repo,
            source_workflow_id=source_workflow_id,
        )
        wid, vid, slug = upsert_workflow(
            mcp, g["name"], g["slug"], g["description"], g["spec"], g.get("tags") or []
        )
        # Publishing is not proof that the server kept the graph we sent.
        # Read it again only after publication and stop before persisting any
        # success-shaped result if normalization disagrees.
        parity = verify_workflow_parity(mcp, wid, g["spec"], slug)
        published[stem] = {"workflow_id": wid, "workflow_version_id": vid, "slug": slug, "parity": parity}
        if stem == "pre-pr-build":
            source_workflow_id = wid
    return published


def persist_published(install: dict, published: dict[str, dict]) -> None:
    mappings = {
        "pr-drive": "pr_drive",
        "pr-status": "pr_status",
        "verify-stack": "verify_stack",
        "implement-slice": "implement_slice",
        "visual-evidence": "visual_evidence",
        "visual-iteration": "visual_iteration",
        "pre-pr-build": "pre_pr_build",
        "build-completion-supervisor": "build_completion_supervisor",
        "pr-lifecycle": "pr_lifecycle",
    }
    for stem, key in mappings.items():
        if stem in published:
            install[key] = {**(install.get(key) or {}), **published[stem]}


def upsert_workflow(mcp: str, name: str, slug: str, description: str, spec: dict, tags: list[str]):
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
        st, patched = api(mcp, "PATCH", f"/workflows/{wid}/versions/{vid}", {"spec": spec, "tags": tags})
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
    # The create endpoint accepts workflow identity only. Tags belong to the
    # version PATCH below; sending them here is rejected before a draft exists.
    st, created = api(
        mcp, "POST", "/workflows", {"name": name, "description": description}
    )
    must(st, created, label="create workflow")
    wid = created["id"]
    vid = (created.get("currentVersion") or created.get("current_version") or {}).get("id")
    if not vid:
        raise SystemExit(f"no version id {created}")
    st, patched = api(mcp, "PATCH", f"/workflows/{wid}/versions/{vid}", {"spec": spec, "tags": tags})
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
    path = HOME / "rewst-install.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(install, indent=2) + "\n")


def main():
    global TENANT
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="pr-drive", help="graph file stem, or 'all'")
    ap.add_argument("--no-run", action="store_true")
    args = ap.parse_args()
    install = load_install()
    TENANT = tenant_id(install)
    instance = install.get("instance_id") or ""
    if not instance:
        raise SystemExit("rewst-install.json missing instance_id")
    hook_secret = os.environ.get("GRAPHWING_HOOK_SECRET") or install.get("hook_secret") or ""
    integration = install.get("custom_integration_id")
    stems = resolve_stems(args.only)
    status_repo = (
        os.environ.get("GRAPHWING_STATUS_REPO")
        or install.get("pr_status_repo")
        or install.get("smoke_repo")
        or ""
    ).strip()
    if "pr-status" in stems and not status_repo:
        raise SystemExit(
            "publishing pr-status requires GRAPHWING_STATUS_REPO, pr_status_repo, or smoke_repo"
        )
    mcp = rewst_mcp(install)
    print("openapi", public_openapi_url())
    published = publish_selected(mcp, install, stems, instance, hook_secret, status_repo)
    if not args.no_run and "pr-drive" in published:
        repo = (os.environ.get("GRAPHWING_SMOKE_REPO") or install.get("smoke_repo") or "").strip()
        if not repo:
            print("skip smoke run (set GRAPHWING_SMOKE_REPO or smoke_repo in rewst-install.json)")
        else:
            rs, rid, _, _ = run_slug(
                mcp,
                "graphwing-pr-drive",
                {"input": {"repo": repo}},
                wait=60,
            )
            published["pr-drive"]["run_id"] = rid
            published["pr-drive"]["status"] = rs
            published["pr-drive"]["smoke"] = "missing_pr"
    install["custom_integration_id"] = integration
    persist_published(install, published)
    save_install(install)
    print("=== DONE ===")
    print(json.dumps(published, indent=2))


if __name__ == "__main__":
    main()
