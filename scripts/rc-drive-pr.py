#!/usr/bin/env python3
"""Operator entry point for one run-control pr-drive lifecycle.

Run 0 of `graphwing-pr-drive-run-control` (see docs/notes/run-control-activation-
recovery.md, "Enabled shape") has no predecessor to reserve it: an operator has
to initialize the run-control record, reserve (state) the first attempt,
consume the authorization, then start the sibling with the four-field
continuity it carries forward. This script does that in one shot:

  python3 scripts/rc-drive-pr.py 3526 --repo riftwing --test riftwing-local-gates

  python3 scripts/rc-drive-pr.py 3526 --repo riftwing --work-kind go_coding \\
      --kick --attempts 3 --turns 150 --wall-seconds 3600 --tokens 6000000 \\
      --cost 75 --wait 1500

Steps: resolve the candidate route from the local daemon (`/v1/slice/route`),
read the PR's head branch/sha with `gh pr view` in the allowlisted repo,
start `graphwing-run-control-initialize`, `graphwing-run-control-state`,
`graphwing-run-control-consume`, then `graphwing-pr-drive-run-control` itself
with the run-control identity the state leg reserved. Each Graph start is a
`POST /workflows/{slug}/run` through `scripts/publish_graphs.py`'s `api` and
`run_slug` helpers; every one of those calls needs the Rewst MCP token, which
`publish_graphs.rewst_mcp` resolves from `GRAPHWING_REWST_MCP_TOKEN` or from
`mcp_bws_key` in rewst-install.json plus `BWS_ACCESS_TOKEN` -- the operator
running this script exports `BWS_ACCESS_TOKEN` from `~/.config/bws/access_token`
first. Subworkflow children take minutes; `--wait` (default 1500s) is the poll
budget for each of the four Graph starts, not the whole lifecycle.

`--kick` records this run's `pr_drive_run_control_hook_url` and `hook_secret`
(from rewst-install.json) as the reservation's kick target and token, so a
red PR can chain into a second, kicked run. Without it the reservation still
binds an endpoint and a request-body hash, but kick_url/kick_token are empty
and the writer's continue leg (if it fires) has nothing to kick.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_graphs as pg  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DAEMON_BASE = "http://127.0.0.1:8645"


# --------------------------------------------------------------------------
# Canonical hashing. Every run-control descriptor hash in the catalog uses
# sort_keys + compact separators (server.py, e.g. run_control_settle's
# receipt hash); the operator has to reproduce it byte for byte.
# --------------------------------------------------------------------------

def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def evaluator_contract_sha256(openapi_path: Path = ROOT / "openapi.json") -> str:
    spec = json.loads(openapi_path.read_text())
    schema = spec["components"]["schemas"]["RunControlRequest"]
    return sha256_hex(schema)


# --------------------------------------------------------------------------
# Pure payload builders. Each one takes plain values, not files or network
# state, so RcDrivePrScriptTests can pin their field sets and hashes without
# a daemon, a repo, or a Rewst token.
# --------------------------------------------------------------------------

def root_workflow_run_id(pr: str, timestamp: str) -> str:
    return f"rc-{pr}-{timestamp}"


def utc_compact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_root_identity(root_workflow_id: str, root_workflow_version_id: str,
                         run_id: str) -> dict[str, str]:
    return {
        "root_workflow_id": root_workflow_id,
        "root_workflow_version_id": root_workflow_version_id,
        "root_workflow_run_id": run_id,
        "purpose": "pr_drive",
    }


def build_budgets(attempts: int, turns: int, wall_seconds: int, tokens: int,
                   provider_cost_usd: str) -> dict[str, Any]:
    return {
        "attempts": attempts,
        "turns": turns,
        "wall_seconds": wall_seconds,
        "tokens": tokens,
        "provider_cost_usd": provider_cost_usd,
    }


def build_next_envelope(turns: int, wall_seconds: int, tokens: int,
                         provider_cost_usd: str) -> dict[str, Any]:
    return {
        "turns": turns,
        "wall_seconds": wall_seconds,
        "tokens": tokens,
        "provider_cost_usd": provider_cost_usd,
    }


def build_initialize_input(root_identity: dict, budgets: dict, initial_route: dict,
                            evaluator_contract_sha256_value: str) -> dict[str, Any]:
    return {
        "root_identity": root_identity,
        "budgets": budgets,
        "initial_route": initial_route,
        "evaluator_contract_sha256": evaluator_contract_sha256_value,
    }


def build_task_material(repo: str, pr: str, test: str) -> dict[str, str]:
    # Same shape as graphs/pr-drive-run-control.json's rc_task_material.task,
    # so a live daemon comparing task_sha256 sees the same fingerprint an
    # in-graph continue leg would have produced for this attempt.
    return {"kind": "pr-drive", "repo": repo, "pr_number": str(pr), "test": test}


def build_callback_material(kick_url: str) -> dict[str, str]:
    return {"kick_url": kick_url}


def build_pr_drive_fields(repo: str, pr: str, test: str, attempt: str, max_attempts: str,
                           auto_merge: bool, kick_url: str, kick_token: str, work_kind: str,
                           commit_message: str) -> dict[str, Any]:
    """The graphwing-pr-drive-run-control `input` fields, minus `run_control`.

    `run_control` is added once the state leg has reserved attempt_id and
    authorization_id and returned the descriptor hash (see main()); it cannot
    be part of this dict up front.
    """
    return {
        "repo": repo,
        "pr_number": str(pr),
        "test": test,
        "attempt": str(attempt),
        "max_attempts": str(max_attempts),
        "auto_merge": "true" if auto_merge else "false",
        "kick_url": kick_url,
        "kick_token": kick_token,
        "class": "mechanical",
        "size": "M",
        "work_kind": work_kind,
        "commit_message": commit_message,
    }


def compute_exact_request_body_sha256(fields: dict[str, Any]) -> str:
    """sha256 of the canonical `{"input": ...}` body this script will POST to
    `/workflows/graphwing-pr-drive-run-control/run`, minus `kick_token` (a
    secret never belongs in a durable descriptor) and minus `run_control`
    (which does not exist yet -- see build_pr_drive_fields)."""
    hashed = {k: v for k, v in fields.items() if k != "kick_token"}
    return sha256_hex({"input": hashed})


def build_state_input(run_control_id: str, candidate_route: dict, next_envelope: dict,
                       launcher_fingerprint: str, endpoint: str, exact_request_body_sha256_value: str,
                       repository: str, branch: str, head_sha: str, task_sha256_value: str,
                       permission_profile: str, callback_binding_sha256_value: str,
                       handoff: dict) -> dict[str, Any]:
    return {
        "run_control_id": run_control_id,
        "candidate_route": candidate_route,
        "next_envelope": next_envelope,
        "launcher_fingerprint": launcher_fingerprint,
        "endpoint": endpoint,
        "exact_request_body_sha256": exact_request_body_sha256_value,
        "repository": repository,
        "branch": branch,
        "head_sha": head_sha,
        "task_sha256": task_sha256_value,
        "permission_profile": permission_profile,
        "callback_binding_sha256": callback_binding_sha256_value,
        "handoff": handoff,
    }


def build_consume_input(run_control_id: str) -> dict[str, str]:
    return {"run_control_id": run_control_id}


def build_start_body(fields: dict[str, Any], run_control: dict[str, str]) -> dict[str, Any]:
    return {"input": {**fields, "run_control": run_control}}


# --------------------------------------------------------------------------
# Trace reading. /runs/{id}/trace nests each node's record at
# data.result.trace, a list of {"nodeId": ..., "output": ...} entries --
# not the top-level {"trace"/"tasks"} id/status shape scripts/drive-pr.py's
# summarize() guesses at. Fall back to that shape too so this still prints
# something useful if the two scripts turn out to disagree about it.
# --------------------------------------------------------------------------

def trace_entries(trace: Any) -> list:
    if not isinstance(trace, dict):
        return []
    nested = (((trace.get("data") or {}).get("result") or {}).get("trace"))
    if isinstance(nested, list):
        return nested
    flat = trace.get("trace") or trace.get("tasks")
    if isinstance(flat, list):
        return flat
    return []


def node_output(trace: Any, node_id: str) -> Any:
    for entry in trace_entries(trace):
        if isinstance(entry, dict) and (entry.get("nodeId") or entry.get("id")) == node_id:
            return entry.get("output")
    return None


def summarize(trace: Any) -> None:
    """One line per node: what ran, in order, and how it ended."""
    entries = trace_entries(trace)
    if not entries:
        print("trace shape unexpected:", json.dumps(trace)[:400])
        return
    for t in entries:
        if not isinstance(t, dict):
            continue
        name = t.get("nodeId") or t.get("id") or t.get("label") or "?"
        status = t.get("status") or t.get("state") or "?"
        err = t.get("error") or ""
        line = "  %-24s %s" % (name, status)
        if err:
            line += "  " + str(err)[:160]
        print(line)


# --------------------------------------------------------------------------
# Local, network-free helpers.
# --------------------------------------------------------------------------

def load_repos() -> dict[str, str]:
    path = pg.HOME / "repos.json"
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text().strip() or "{}")
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} must be a JSON object")
    return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip() and str(v).strip()}


def resolve_repo_path(name: str, repos: dict[str, str]) -> Path:
    if name not in repos:
        raise SystemExit(f"unknown repo '{name}'; allowed: {sorted(repos)} (see {pg.HOME / 'repos.json'})")
    path = Path(repos[name])
    if not path.is_dir():
        raise SystemExit(f"repo path missing: {path}")
    return path


def api_key() -> str:
    path = pg.HOME / "api.key"
    if not path.is_file():
        raise SystemExit(f"missing {path}")
    return path.read_text().strip()


def resolve_route(key: str, work_kind: str) -> dict[str, str]:
    """POST /v1/slice/route on the local daemon; same auth as drive-pr.py's findings()."""
    body = json.dumps({"class": "mechanical", "size": "M", "work_kind": work_kind}).encode()
    req = urllib.request.Request(
        DAEMON_BASE + "/v1/slice/route",
        data=body,
        method="POST",
        headers={"X-Graphwing-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"slice route failed: HTTP {exc.code} {exc.read().decode()[:400]}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"slice route failed: {exc}")
    return {
        "route_version": data["route_version"],
        "launcher": data["launcher"],
        "provider": data["provider"],
        "model": data["model"],
    }


def gh_pr_view(repo_path: Path, pr: str) -> tuple[str, str]:
    proc = subprocess.run(
        ["gh", "pr", "view", str(pr), "--json", "headRefName,headRefOid"],
        cwd=str(repo_path), capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise SystemExit(f"gh pr view {pr} failed: {proc.stderr.strip()[:400]}")
    data = json.loads(proc.stdout)
    return data["headRefName"], data["headRefOid"]


# --------------------------------------------------------------------------
# workflow-runs.jsonl, same shape as server.py's record_workflow_run.
# --------------------------------------------------------------------------

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def workflow_run_title(workflow: str, inp: dict[str, Any]) -> str:
    pr = inp.get("pr")
    if pr not in (None, ""):
        return f"{workflow} PR {pr}"
    repo = str(inp.get("repo") or "").strip()
    if repo:
        return f"{workflow} {repo}"
    return workflow


def record_workflow_run(row: dict[str, Any]) -> None:
    payload = {
        "workflow": str(row.get("workflow") or ""),
        "status": str(row.get("status") or "fired"),
        "source": str(row.get("source") or "fire"),
        "input": row.get("input") if isinstance(row.get("input"), dict) else {},
        "run_id": row.get("run_id"),
        "created_at": str(row.get("created_at") or utcnow()),
    }
    if row.get("run_control_id"):
        payload["run_control_id"] = row["run_control_id"]
    payload["title"] = workflow_run_title(payload["workflow"], payload["input"])
    path = pg.HOME / "workflow-runs.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
    except OSError as exc:
        print("could not record workflow run:", exc)


def resolved(value, default):
    return value if value is not None else default


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pr")
    ap.add_argument("--repo", default="riftwing")
    ap.add_argument("--test", default="riftwing-local-gates")
    ap.add_argument("--work-kind", dest="work_kind", default="go_coding")
    ap.add_argument("--kick", action="store_true",
                     help="record the pr-drive-run-control hook as this reservation's kick target")
    ap.add_argument("--attempts", type=int, default=None, help="budgets.attempts (default 3)")
    ap.add_argument("--turns", type=int, default=None,
                     help="budgets.turns (default 150) and next_envelope.turns (default 50)")
    ap.add_argument("--wall-seconds", dest="wall_seconds", type=int, default=None,
                     help="budgets.wall_seconds (default 3600) and next_envelope.wall_seconds (default 660)")
    ap.add_argument("--tokens", type=int, default=None,
                     help="budgets.tokens (default 6000000) and next_envelope.tokens (default 2000000)")
    ap.add_argument("--cost", default=None,
                     help="budgets.provider_cost_usd (default 75) and next_envelope.provider_cost_usd (default 25)")
    ap.add_argument("--wait", type=int, default=1500,
                     help="seconds to poll each of the four Graph starts (default 1500)")
    return ap


def main() -> int:
    args = build_arg_parser().parse_args()

    install = pg.load_install()
    pg.TENANT = pg.tenant_id(install)
    mcp = pg.rewst_mcp(install)

    pr_drive_run_control = install.get("pr_drive_run_control") or {}
    workflow_id = pr_drive_run_control.get("workflow_id")
    workflow_version_id = pr_drive_run_control.get("workflow_version_id")
    if not workflow_id or not workflow_version_id:
        raise SystemExit(
            "install['pr_drive_run_control'] has no workflow_id/workflow_version_id; "
            "publish it first (scripts/publish_graphs.py --only pr-drive-run-control)"
        )

    print("=== resolving route ===")
    route = resolve_route(api_key(), args.work_kind)
    print("route", json.dumps(route))

    print("=== reading PR head ===")
    repos = load_repos()
    repo_path = resolve_repo_path(args.repo, repos)
    branch, head_sha = gh_pr_view(repo_path, args.pr)
    print("branch", branch, "head_sha", head_sha)

    budgets = build_budgets(
        resolved(args.attempts, 3), resolved(args.turns, 150),
        resolved(args.wall_seconds, 3600), resolved(args.tokens, 6000000),
        resolved(args.cost, "75"),
    )
    next_envelope = build_next_envelope(
        resolved(args.turns, 50), resolved(args.wall_seconds, 660),
        resolved(args.tokens, 2000000), resolved(args.cost, "25"),
    )

    print("=== graphwing-run-control-initialize ===")
    root_identity = build_root_identity(
        workflow_id, workflow_version_id, root_workflow_run_id(args.pr, utc_compact_timestamp()),
    )
    initialize_input = build_initialize_input(
        root_identity, budgets, route, evaluator_contract_sha256(),
    )
    status, rid, _run, trace = pg.run_slug(
        mcp, "graphwing-run-control-initialize", {"input": initialize_input}, wait=args.wait,
    )
    print("run", rid, "->", status)
    summarize(trace)
    identity = node_output(trace, "identity")
    if not isinstance(identity, dict) or not identity.get("run_control_id"):
        raise SystemExit("initialize did not yield identity.run_control_id in the trace")
    run_control_id = identity["run_control_id"]
    print("run_control_id", run_control_id)

    print("=== graphwing-run-control-state ===")
    kick_url = str(install.get("pr_drive_run_control_hook_url") or "") if args.kick else ""
    kick_token = str(install.get("hook_secret") or "") if args.kick else ""
    if args.kick and (not kick_url or not kick_token):
        raise SystemExit(
            "--kick requires pr_drive_run_control_hook_url and hook_secret in rewst-install.json"
        )
    fields = build_pr_drive_fields(
        args.repo, args.pr, args.test, "1", str(budgets["attempts"]),
        False, kick_url, kick_token, args.work_kind, "fix: address review findings",
    )
    exact_request_body_sha256_value = compute_exact_request_body_sha256(fields)
    task_sha256_value = sha256_hex(build_task_material(args.repo, args.pr, args.test))
    callback_binding_sha256_value = sha256_hex(build_callback_material(kick_url))
    state_input = build_state_input(
        run_control_id, route, next_envelope, "graphwing-operator-start-v1",
        "/workflows/graphwing-pr-drive-run-control/run", exact_request_body_sha256_value,
        args.repo, branch, head_sha, task_sha256_value, "pr-drive-writer-v1",
        callback_binding_sha256_value, {"reason_code": "provider_switch"},
    )
    status, rid, _run, trace = pg.run_slug(
        mcp, "graphwing-run-control-state", {"input": state_input}, wait=args.wait,
    )
    print("run", rid, "->", status)
    summarize(trace)
    attempt_identity = node_output(trace, "attempt_identity")
    descriptor_hash = node_output(trace, "descriptor_hash")
    if not isinstance(attempt_identity, dict) or not attempt_identity.get("attempt_id") \
            or not attempt_identity.get("authorization_id"):
        raise SystemExit("state did not yield attempt_identity in the trace")
    if not isinstance(descriptor_hash, dict) or not descriptor_hash.get("value"):
        raise SystemExit("state did not yield descriptor_hash.value in the trace")
    attempt_id = attempt_identity["attempt_id"]
    authorization_id = attempt_identity["authorization_id"]
    launch_descriptor_sha256 = descriptor_hash["value"]
    print("attempt_id", attempt_id, "authorization_id", authorization_id)
    print("launch_descriptor_sha256", launch_descriptor_sha256)

    print("=== graphwing-run-control-consume ===")
    status, rid, _run, trace = pg.run_slug(
        mcp, "graphwing-run-control-consume", {"input": build_consume_input(run_control_id)},
        wait=args.wait,
    )
    print("run", rid, "->", status)
    summarize(trace)

    print("=== graphwing-pr-drive-run-control ===")
    run_control = {
        "run_control_id": run_control_id,
        "attempt_id": attempt_id,
        "authorization_id": authorization_id,
        "launch_descriptor_sha256": launch_descriptor_sha256,
    }
    start_body = build_start_body(fields, run_control)
    print("input", json.dumps(start_body["input"]))
    status, rid, _run, trace = pg.run_slug(
        mcp, "graphwing-pr-drive-run-control", start_body, wait=args.wait,
    )
    print("run", rid, "->", status)
    print("nodes:")
    summarize(trace)
    print("settle result:", json.dumps(node_output(trace, "rc_settle")))
    print("reconcile child status:", json.dumps(node_output(trace, "rc_reconcile")))

    record_workflow_run({
        "workflow": "pr-drive-run-control",
        "status": status,
        "source": "rc-operator",
        "input": {"repo": args.repo, "pr": str(args.pr), "test": args.test,
                   "class": "mechanical", "size": "M"},
        "run_id": rid,
        "run_control_id": run_control_id,
        "created_at": utcnow(),
    })

    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
