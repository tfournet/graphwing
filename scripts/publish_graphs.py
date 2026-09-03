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

# docs/ARCHITECTURE.md + issue #184. Keep this inventory complete until the
# separately gated v2 cutover removes the compatibility owners.
RUN_CONTROL_BASELINE_SHA = "631e10f9fa5ae4aa7a0ae511161e1a7faab99016"
RUN_CONTROL_GRAPH_CLASSIFICATION = {
    "run-control-authorize": "compatibility-only",
    "run-control-consume": "active",
    "run-control-consume-authorization": "active",
    "run-control-initialize": "active",
    "run-control-reconcile": "active",
    "run-control-state": "active",
    "run-control-transition": "active",
}
REWST_AUTHORIZATION_PRIMITIVES = (
    "_validate_rewst_consumed_authorization",
    "claim_rewst_launch_authority",
    "consume_rewst_authority_for_job",
    "consume_rewst_launch_authority",
    "rewst_descriptor_matches",
    "verify_rewst_issuer_request",
)
REWST_AUTHORIZATION_FOUNDATION = (
    "_new_rewst_server_instance_challenge",
    *REWST_AUTHORIZATION_PRIMITIVES,
)
RUN_CONTROL_WORKFLOW_PIN_SOURCES = {
    "$GRAPHWING_RUN_CONTROL_AUTHORIZE": "run-control-consume-authorization",
    "$GRAPHWING_RUN_CONTROL_CONSUME": "run-control-consume",
    "$GRAPHWING_RUN_CONTROL_RECONCILE": "run-control-reconcile",
    "$GRAPHWING_RUN_CONTROL_STATE": "run-control-state",
    "$GRAPHWING_RUN_CONTROL_TRANSITION": "run-control-transition",
}
ROUTING_POLICY_WORKFLOW_PIN_SOURCES = {
    "$GRAPHWING_ROUTING_POLICY_WORKFLOW_ID": "routing-policy",
    "$GRAPHWING_ROUTING_POLICY_VERSION_ID": "routing-policy",
}
RUN_CONTROL_PIN_ERROR_MESSAGES = {
    "run-control-consume-authorization": "run-control authorization publication returned no exact IDs",
    "run-control-transition": "run-control transition publication returned no exact IDs",
}
RUN_CONTROL_DAEMON_CLASSIFICATION = {
    # Daemon-owned aggregate budgets, history interpretation, and next state.
    "RUN_CONTROL_VERSION": "business-policy",
    "RUN_CONTROL_MAX_ATTEMPTS": "business-policy",
    "RUN_CONTROL_CONSTRAINT_SIGNALS": "business-policy",
    "RUN_CONTROL_GAMING_SIGNALS": "business-policy",
    "RUN_CONTROL_BUDGET_FIELDS": "business-policy",
    "RUN_CONTROL_ATTEMPT_FIELDS": "business-policy",
    "RUN_CONTROL_PROGRESS_FIELDS": "business-policy",
    "RUN_CONTROL_REQUEST_FIELDS": "business-policy",
    "RUN_CONTROL_NEXT_ENVELOPE_FIELDS": "business-policy",
    "_run_control_checkpoint": "business-policy",
    "run_control_evaluate": "business-policy",

    # Closed parsing and projections that do not choose the next state.
    "RUN_CONTROL_ROUTE_FIELDS": "normalized-fact",
    "RUN_CONTROL_USAGE_FIELDS": "normalized-fact",
    "RUN_CONTROL_ID_RE": "normalized-fact",
    "RUN_CONTROL_REWST_ID_RE": "normalized-fact",
    "RUN_CONTROL_RUN_ID_RE": "normalized-fact",
    "RUN_CONTROL_ATTEMPT_ID_RE": "normalized-fact",
    "RUN_CONTROL_COST_RE": "normalized-fact",
    "_run_control_usage_projection": "normalized-fact",
    "_run_control_receipt_sha256": "normalized-fact",
    "_run_control_error": "normalized-fact",
    "_run_control_closed": "normalized-fact",
    "_run_control_text": "normalized-fact",
    "_run_control_uint": "normalized-fact",
    "_run_control_cost": "normalized-fact",
    "_run_control_cost_text": "normalized-fact",
    "_run_control_ratio_text": "normalized-fact",
    "_run_control_route": "normalized-fact",
    "run_control_validate_initialize": "normalized-fact",
    "run_control_validate_receipt": "normalized-fact",

    # Active non-bypassable request/continuity fences.
    "RUN_CONTROL_CONTINUITY_FIELDS": "hard-safety",
    "_new_rewst_server_instance_challenge": "hard-safety",
    "run_control_authorization_id": "hard-safety",
    "run_control_continuity": "hard-safety",
    "run_control_kick_request_sha256": "hard-safety",
    "run_control_claim_kick": "hard-safety",
    "run_control_claim_launch": "hard-safety",
    "run_control_record_kick_result": "hard-safety",

    # v1 local coordination and settlement retained for in-flight records.
    "RUN_CONTROL_DIR": "compatibility-only",
    "RUN_CONTROL_SETTLEMENT_VERSION": "compatibility-only",
    "RUN_CONTROL_LEDGER_VERSION": "compatibility-only",
    "RUN_CONTROL_KICK_LOSS_GRACE_SECONDS": "compatibility-only",
    "RUN_CONTROL_AUTHORITY_LOSS_REASONS": "compatibility-only",
    "RUN_CONTROL_SETTLE_FIELDS": "compatibility-only",
    "RUN_CONTROL_TERMINAL_FAILURE_CODES": "compatibility-only",
    "RUN_CONTROL_LEDGER_LOCK": "compatibility-only",
    "_run_control_now": "compatibility-only",
    "_run_control_ledger_path": "compatibility-only",
    "_run_control_job_path": "compatibility-only",
    "_run_control_ledger_read": "compatibility-only",
    "_run_control_ledger_write": "compatibility-only",
    "_run_control_ledger_record": "compatibility-only",
    "_run_control_record_continuity": "compatibility-only",
    "_run_control_settled_receipt": "compatibility-only",
    "_run_control_settlement_result": "compatibility-only",
    "run_control_settle": "compatibility-only",
    "run_control_authority_loss": "compatibility-only",

    # Unreferenced helper and exact HMAC/descriptor/one-time launch authority.
    "_run_control_id": "dormant",
    **{symbol: "hard-safety" for symbol in REWST_AUTHORIZATION_PRIMITIVES},
    "consume_rewst_authority_for_job": "dormant",
}


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


def subst_instance(obj, instance: str, hook_secret: str = "", status_repo: str = "",
                   workflow_pins: dict[str, str] | None = None):
    workflow_pins = workflow_pins or {}
    if isinstance(obj, str):
        value = (
            obj.replace("$GRAPHWING_INSTANCE", instance)
            .replace("$GRAPHWING_HOOK_SECRET", hook_secret)
            .replace("$GRAPHWING_STATUS_REPO", status_repo)
        )
        for placeholder, exact_id in workflow_pins.items():
            value = value.replace(placeholder, exact_id)
        return value
    if isinstance(obj, list):
        return [subst_instance(x, instance, hook_secret, status_repo, workflow_pins) for x in obj]
    if isinstance(obj, dict):
        return {k: subst_instance(v, instance, hook_secret, status_repo, workflow_pins)
                for k, v in obj.items()}
    return obj


def assert_pinned_subworkflows(graph: dict) -> None:
    for node in graph.get("spec", {}).get("nodes", []):
        if node.get("type") != "action.subworkflow":
            continue
        config = node.get("config") or {}
        if not config.get("workflowId") or not config.get("workflowVersionId"):
            raise SystemExit(f"{graph.get('slug')}:{node.get('id')} lacks exact child pin")
        if "versionSelection" in config or "$GRAPHWING_" in json.dumps(config):
            raise SystemExit(f"{graph.get('slug')}:{node.get('id')} has unresolved/latest child")


def load_graph(name: str, instance: str, hook_secret: str = "", status_repo: str = "",
               workflow_pins: dict[str, str] | None = None) -> dict:
    raw = json.loads((GRAPHS / f"{name}.json").read_text())
    graph = subst_instance(raw, instance, hook_secret, status_repo, workflow_pins)
    if workflow_pins is not None:
        assert_pinned_subworkflows(graph)
    return graph


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


def read_back_exact_published_version(
    mcp: str, workflow_id: str, version_id: str, slug: str,
) -> str:
    """Require a fresh provider readback to name the exact published version."""
    st, workflow = api(mcp, "GET", f"/workflows/{workflow_id}?include=spec")
    must(st, workflow, label=f"read back {slug}")
    if not isinstance(workflow, dict):
        raise SystemExit(f"{slug} readback was not an object")
    current = workflow.get("currentVersion") or workflow.get("current_version") or {}
    if (
        workflow.get("id") != workflow_id
        or current.get("id") != version_id
        or current.get("status") != "published"
    ):
        raise SystemExit(
            f"{slug} readback did not match exact published version {version_id}"
        )
    return version_id


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


CATALOG = [
    "routing-policy", "run-control-transition", "run-control-consume-authorization",
    "run-control-initialize", "run-control-state", "run-control-reconcile",
    "run-control-consume", "run-control-authorize", "verify-stack",
    "implement-slice", "pr-drive", "pr-status", "code-off",
]
# pr-drive embeds exact run-control child pins (every attempt reserves and
# settles in-run), so publishing it by name republishes its durable chain
# first so no stale child version can be pinned.
DURABLE_CHAIN = [
    "run-control-transition", "run-control-consume-authorization",
    "run-control-state", "run-control-consume", "run-control-reconcile",
]


def publish_stems(only: str) -> list[str]:
    if only == "all":
        return list(CATALOG)
    if only == "pr-drive":
        return ["routing-policy"] + DURABLE_CHAIN + ["pr-drive"]
    if only == "implement-slice":
        return ["routing-policy", "implement-slice"]
    return [only]


def register_run_control_workflow_pins(
    stem: str, workflow_id, version_id, pins: dict[str, str],
) -> None:
    placeholders = [
        placeholder for placeholder, source in RUN_CONTROL_WORKFLOW_PIN_SOURCES.items()
        if source == stem
    ]
    if not placeholders:
        return
    if not isinstance(workflow_id, str) or not isinstance(version_id, str) or not workflow_id or not version_id:
        raise SystemExit(RUN_CONTROL_PIN_ERROR_MESSAGES.get(stem, f"{stem} publication returned no exact IDs"))
    for placeholder in placeholders:
        pins[f"{placeholder}_WORKFLOW_ID"] = workflow_id
        pins[f"{placeholder}_VERSION_ID"] = version_id


def register_routing_policy_workflow_pins(
    stem: str, workflow_id, version_id, pins: dict[str, str],
) -> None:
    placeholders = [
        placeholder for placeholder, source in ROUTING_POLICY_WORKFLOW_PIN_SOURCES.items()
        if source == stem
    ]
    if not placeholders:
        return
    if not isinstance(workflow_id, str) or not isinstance(version_id, str) or not workflow_id or not version_id:
        raise SystemExit("routing policy publication returned no exact IDs")
    for placeholder in placeholders:
        pins[placeholder] = workflow_id if placeholder.endswith("WORKFLOW_ID") else version_id


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
    stems = publish_stems(args.only)
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
    published = {}
    workflow_pins: dict[str, str] = {}
    for stem in stems:
        g = load_graph(stem, instance, hook_secret, status_repo, workflow_pins)
        wid, vid, slug = upsert_workflow(mcp, g["name"], g["slug"], g["description"], g["spec"])
        published[stem] = {"workflow_id": wid, "workflow_version_id": vid, "slug": slug}
        register_run_control_workflow_pins(stem, wid, vid, workflow_pins)
        register_routing_policy_workflow_pins(stem, wid, vid, workflow_pins)
        if stem == "routing-policy":
            if not isinstance(wid, str) or not isinstance(vid, str):
                raise SystemExit("routing policy publication returned no exact IDs")
            read_back_exact_published_version(mcp, wid, vid, slug)
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
    if "routing-policy" in published:
        install["routing_policy"] = published["routing-policy"]
    install["pr_drive"] = published.get("pr-drive") or install.get("pr_drive")
    if "pr-status" in published:
        install["pr_status"] = published["pr-status"]
    if "verify-stack" in published:
        install["verify_stack"] = {**(install.get("verify_stack") or {}), **published["verify-stack"]}
    if "implement-slice" in published:
        install["implement_slice"] = {**(install.get("implement_slice") or {}), **published["implement-slice"]}
    if "code-off" in published:
        install["code_off"] = {**(install.get("code_off") or {}), **published["code-off"]}
    if "run-control-authorize" in published:
        install["run_control_authorize"] = published["run-control-authorize"]
    if "run-control-transition" in published:
        install["run_control_transition"] = published["run-control-transition"]
    if "run-control-state" in published:
        install["run_control_state"] = published["run-control-state"]
    if "run-control-initialize" in published:
        install["run_control_initialize"] = published["run-control-initialize"]
    if "run-control-reconcile" in published:
        install["run_control_reconcile"] = published["run-control-reconcile"]
    if "run-control-consume-authorization" in published:
        install["run_control_consume_authorization"] = published["run-control-consume-authorization"]
    if "run-control-consume" in published:
        install["run_control_consume"] = published["run-control-consume"]
    save_install(install)
    print("=== DONE ===")
    print(json.dumps(published, indent=2))


if __name__ == "__main__":
    main()
