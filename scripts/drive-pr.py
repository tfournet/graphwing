#!/usr/bin/env python3
"""Drive a PR to green through graphwing-pr-drive, and print the node trace.

Every attempt is now run-control reservation-backed and the retry loop lives
inside the graph (graphs/pr-drive.json's capped `attempts` logic.loop), not
here. This script resolves the exact workflow policy, initializes the run-control
record, and starts the graph. One call, one run, up to three in-graph attempts.

Why REST and not the webhook: a webhook-triggered run leaves CTX.INPUT empty,
so every {{ CTX.INPUT.* }} in the graph resolves to nothing and pr-drive dies at
ghPrView with no `number` on the query string. That failure is invisible from
outside — `repo` still arrives, because openapi.json declares a default of
"riftwing" for it, so the request looks half-right. POST /workflows/{slug}/run
passes inputs properly; it is how implement-slice has always been fired.

  python3 scripts/drive-pr.py 3526 [--repo riftwing] [--test riftwing-local-gates]
                                   [--auto-merge] [--wait 900]

Budget it end to end before wrapping this in anything with its own timeout: the
whole retry chain is now one Graph run, up to three in-graph attempts plus one
final test. --wait defaults to that run's own worst case, derived from the
graph rather than guessed (see worst_case_run_seconds).

Known gap, not built here: between one in-graph attempt's push and the next
attempt's findings read, nothing waits for the external reviewer to react to
the new commit (the old cross-run driver had a wait_for_fresh_review step
between separate Python-level attempts; an in-graph loop has no equivalent
poll-until-changed primitive without its own bounded sub-loop). A slot can
therefore read a still-stale blocking finding right after a push. Only the
run's own entry point waits for a settled audit, below.

Needs the Rewst MCP token: GRAPHWING_REWST_MCP_TOKEN, or mcp_bws_key in
rewst-install.json plus BWS_ACCESS_TOKEN.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_graphs as pg  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import parse_route_execution_profile, resolve_executable  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DAEMON_BASE = "http://127.0.0.1:8645"


# --------------------------------------------------------------------------
# Canonical hashing, same convention as every run-control descriptor hash in
# the catalog (server.py, e.g. run_control_settle's receipt hash).
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
# Pure payload builders, pinned by DrivePrScriptTests without a daemon, a
# repo, or a Rewst token.
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


def build_initialize_input(root_identity: dict, budgets: dict, initial_route: dict,
                            evaluator_contract_sha256_value: str) -> dict[str, Any]:
    return {
        "root_identity": root_identity,
        "budgets": budgets,
        "initial_route": initial_route,
        "evaluator_contract_sha256": evaluator_contract_sha256_value,
    }


def build_pr_drive_input(repo: str, pr: str, test: str, auto_merge: bool,
                          klass: str, size: str, work_kind: str, prompt: str,
                          commit_message: str, run_control_id: str) -> dict[str, Any]:
    """graphwing-pr-drive's `input`. The loop reserves, launches, settles, and
    reconciles up to three attempts against run_control_id; there is no
    attempt/max_attempts/kick_url/kick_token here because nothing crosses runs."""
    return {
        "repo": repo,
        "pr_number": str(pr),
        "test": test,
        "auto_merge": "true" if auto_merge else "false",
        "class": klass,
        "size": size,
        "work_kind": work_kind,
        "prompt": prompt,
        "commit_message": commit_message,
        "run_control_id": run_control_id,
    }


# --------------------------------------------------------------------------
# Trace reading. /runs/{id}/trace nests each node's record at
# data.result.trace, a list of {"nodeId": ..., "output": ...} entries; a
# flatter {"trace"/"tasks"} id/status shape is also accepted defensively.
# --------------------------------------------------------------------------

def trace_entries(trace: Any) -> list:
    if isinstance(trace, list):
        return trace
    if not isinstance(trace, dict):
        return []
    nested = (((trace.get("data") or {}).get("result") or {}).get("trace"))
    if isinstance(nested, list):
        return nested
    flat = trace.get("trace") or trace.get("tasks")
    if isinstance(flat, list):
        return flat
    return []


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


def failed_node(trace):
    """First failed node and its error, so a run says why it stopped."""
    for t in trace_entries(trace):
        if isinstance(t, dict) and (t.get("status") or t.get("state")) in ("failed", "error"):
            return t.get("nodeId") or t.get("id") or "?", str(t.get("error") or "")
    return None


def worst_case_run_seconds() -> int:
    """Longest a single pr-drive run can legitimately take.

    Derived from the graph rather than guessed, because every hardcoded timeout
    in this chain has already drifted behind the work it wraps at least once.
    Body waits (reached from the loop's "each" handle) run once per attempt
    slot; the tail's final_wait runs once after the loop's "done" handle.
    """
    graph = json.loads((ROOT / "graphs" / "pr-drive.json").read_text())["spec"]
    nodes = {n["id"]: n for n in graph["nodes"]}
    adjacency: dict[str, list[tuple[str | None, str]]] = {}
    for edge in graph["edges"]:
        adjacency.setdefault(edge["source"], []).append((edge.get("sourceHandle"), edge["target"]))

    def reachable(source_id: str, handle: str) -> set[str]:
        seen: set[str] = set()
        stack = [target for h, target in adjacency.get(source_id, []) if h == handle]
        while stack:
            current = stack.pop()
            if current in seen or current == source_id:
                continue
            seen.add(current)
            stack.extend(target for _h, target in adjacency.get(current, []))
        return seen

    total = 300  # margin for the non-waiting nodes and gh calls
    for loop_node in graph["nodes"]:
        if loop_node["type"] != "logic.loop":
            continue
        max_iterations = int(loop_node["config"].get("maxIterations") or 1)
        body = reachable(loop_node["id"], "each")
        total += max_iterations * sum(
            nodes[n]["config"]["timeoutSeconds"] for n in body if nodes[n]["type"] == "action.wait.webhook"
        )
        tail = reachable(loop_node["id"], "done")
        total += sum(
            nodes[n]["config"]["timeoutSeconds"] for n in tail if nodes[n]["type"] == "action.wait.webhook"
        )
    return total


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


def resolve_route(mcp: str, policy_ref: dict[str, Any], klass: str, size: str, work_kind: str,
                  wait: int = 90) -> dict[str, str]:
    workflow_id = policy_ref.get("workflow_id")
    version_id = policy_ref.get("workflow_version_id")
    slug = policy_ref.get("slug")
    if not all(isinstance(value, str) and value for value in (workflow_id, version_id, slug)):
        raise SystemExit("install['routing_policy'] has no exact workflow/version/slug; publish it first")
    assert isinstance(workflow_id, str) and isinstance(version_id, str) and isinstance(slug, str)
    if slug != "graphwing-routing-policy":
        raise SystemExit("install['routing_policy'] names the wrong workflow")
    pg.read_back_exact_published_version(mcp, workflow_id, version_id, slug)
    status, _run_id, _run, trace = pg.run_slug(
        mcp, slug,
        {"input": {"class": klass, "size": size, "work_kind": work_kind}},
        wait=wait,
    )
    if status != "completed":
        raise SystemExit(f"routing policy run ended {status}")
    output = next((
        entry.get("output") for entry in trace_entries(trace)
        if isinstance(entry, dict)
        and (entry.get("nodeId") or entry.get("id")) == "route_output"
    ), None)
    if not isinstance(output, dict):
        raise SystemExit("routing policy did not return route_output")
    profile = output.get("writer_execution_profile")
    launcher, provider = output.get("launcher"), output.get("provider")
    model, effort = output.get("model"), output.get("effort")
    if not all(isinstance(value, str) for value in (launcher, provider, model, effort)):
        raise SystemExit("routing policy returned a noncanonical writer profile")
    assert isinstance(launcher, str) and isinstance(provider, str)
    assert isinstance(model, str) and isinstance(effort, str)
    parsed, error = parse_route_execution_profile(
        profile, expected_role="writer", launcher=launcher,
        provider=provider, model=model, effort=effort,
    )
    if (
        error is not None or parsed != profile or not isinstance(profile, dict)
        or output.get("compatibility_behavior") != "normal-v1"
    ):
        raise SystemExit("routing policy returned a noncanonical writer profile")
    return {
        "route_version": output["compatibility_behavior"],
        "launcher": profile["launcher"], "provider": profile["provider"],
        "model": profile["model"],
    }


def gh_pr_view(repo_path: Path, pr: str) -> tuple[str, str]:
    gh_bin = resolve_executable(
        "gh", "GRAPHWING_GH_BIN", Path.home() / ".local" / "bin" / "gh",
    )
    if not gh_bin.is_file() or not os.access(gh_bin, os.X_OK):
        raise SystemExit(f"resolved gh executable is unavailable: {gh_bin}")
    proc = subprocess.run(
        [str(gh_bin), "pr", "view", str(pr), "--json", "headRefName,headRefOid"],
        cwd=str(repo_path), capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise SystemExit(f"gh pr view {pr} failed: {proc.stderr.strip()[:400]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"gh pr view {pr} returned non-JSON output") from None
    return data["headRefName"], data["headRefOid"]


def findings(repo: str, pr: str):
    """Read the PR's machine verdict through the same endpoint the graph uses."""
    key = api_key()
    url = "%s/v1/gh/pr/findings?repo=%s&number=%s" % (DAEMON_BASE, repo, pr)
    req = urllib.request.Request(url, headers={"X-Graphwing-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, ValueError) as exc:
        print("findings read failed:", exc)
        return None


# A full review can take 30 minutes. Starting the run before the audit has
# settled on the current commit means the first in-graph attempt reads
# whatever verdict was already there and can burn an iteration fixing work
# that a still-running review would have cleared on its own.
REVIEW_WAIT_SECONDS = int(os.environ.get("GRAPHWING_REVIEW_WAIT", "2700"))
REVIEW_POLL_SECONDS = 30


def audit_is_in_flight(opening: dict) -> bool:
    """True only while hold:* is present and no grade has settled.

    A returning hold:pm-review after A/A- is merge policy, not an unfinished
    audit. Waiting GRAPHWING_REVIEW_WAIT for it never clears. #177.
    """
    if not opening.get("holds"):
        return False
    grade = opening.get("grade")
    return grade in (None, "")


def wait_for_fresh_review(repo, pr, previous, tries: int = 0, gap: int = 0) -> bool:
    if not audit_is_in_flight(previous):
        return True
    gap = gap or REVIEW_POLL_SECONDS
    tries = tries or max(1, REVIEW_WAIT_SECONDS // gap)
    before = {f.get("fingerprint") for f in (previous.get("findings") or [])}
    for _ in range(tries):
        time.sleep(gap)
        now = findings(repo, pr)
        if now is None:
            return False
        if audit_is_in_flight(now):
            continue
        after = {f.get("fingerprint") for f in (now.get("findings") or [])}
        if after != before or not now.get("blocking") or now.get("grade"):
            print("review re-ran: grade=%s blocking=%s" % (now.get("grade"), now.get("blocking")))
            return True
    print("review did not settle within %ds; stopping rather than acting on a stale read"
          % REVIEW_WAIT_SECONDS)
    return False


# --------------------------------------------------------------------------
# workflow-runs.jsonl, same shape as server.py's record_workflow_run.
# --------------------------------------------------------------------------

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def workflow_run_title(workflow: str, inp: dict[str, Any]) -> str:
    pr = inp.get("pr_number") or inp.get("pr")
    if pr not in (None, ""):
        return f"{workflow} PR {pr}"
    repo = str(inp.get("repo") or "").strip()
    return f"{workflow} {repo}" if repo else workflow


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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pr")
    ap.add_argument("--repo", default="riftwing")
    ap.add_argument("--test", default="riftwing-local-gates")
    ap.add_argument("--auto-merge", action="store_true")
    ap.add_argument("--class", dest="klass", default="mechanical",
                     choices=["mechanical", "visual", "sensitive"])
    ap.add_argument("--size", default="M", choices=["S", "M", "L"])
    ap.add_argument("--work-kind", dest="work_kind", default="go_coding")
    ap.add_argument("--message", default="fix: address review findings",
                     help="commit message; git_commit rejects an empty one")
    ap.add_argument("--attempts", type=int, default=None, help="budgets.attempts (default 3)")
    ap.add_argument("--turns", type=int, default=None, help="budgets.turns (default 150)")
    ap.add_argument("--wall-seconds", dest="wall_seconds", type=int, default=None,
                     help="budgets.wall_seconds (default 3600)")
    ap.add_argument("--tokens", type=int, default=None, help="budgets.tokens (default 6000000)")
    ap.add_argument("--cost", default=None, help="budgets.provider_cost_usd (default 75)")
    ap.add_argument("--wait", type=int, default=0,
                     help="seconds to poll the run; defaults to the graph's own worst case")
    return ap


def main() -> int:
    args = build_arg_parser().parse_args()

    floor = worst_case_run_seconds()
    if not args.wait:
        args.wait = floor
    elif args.wait < floor:
        print("refusing: --wait %ds is below the graph's worst case of %ds. "
              "run_slug would abandon a run that is still working, and the Rewst "
              "run would keep going with nothing watching it." % (args.wait, floor))
        return 1
    print("run budget: up to %ds (%.1f h)" % (args.wait, args.wait / 3600))

    install = pg.load_install()
    pg.TENANT = pg.tenant_id(install)
    mcp = pg.rewst_mcp(install)

    print("=== checking the audit is settled ===")
    opening = findings(args.repo, args.pr)
    if opening is None:
        return 1
    if audit_is_in_flight(opening):
        print("audit is still running on this commit (%s); waiting for a verdict "
              "before starting, or the first attempt works from a stale brief"
              % ", ".join(opening["holds"]))
        if not wait_for_fresh_review(args.repo, args.pr, opening):
            return 1

    print("=== reading the PR head ===")
    repos = load_repos()
    repo_path = resolve_repo_path(args.repo, repos)
    branch, head_sha = gh_pr_view(repo_path, args.pr)
    print("branch", branch, "head_sha", head_sha)

    print("=== resolving the opening route ===")
    initial_route = resolve_route(
        mcp, install.get("routing_policy") or {}, args.klass, args.size, args.work_kind,
    )
    print("initial_route", json.dumps(initial_route))

    budgets = build_budgets(
        resolved(args.attempts, 3), resolved(args.turns, 150),
        resolved(args.wall_seconds, 3600), resolved(args.tokens, 6000000),
        resolved(args.cost, "75"),
    )
    pr_drive = install.get("pr_drive") or {}
    workflow_id, workflow_version_id = pr_drive.get("workflow_id"), pr_drive.get("workflow_version_id")
    if not workflow_id or not workflow_version_id:
        raise SystemExit("install['pr_drive'] has no workflow_id/workflow_version_id; publish it first "
                          "(scripts/publish_graphs.py --only pr-drive)")
    root_identity = build_root_identity(
        workflow_id, workflow_version_id, root_workflow_run_id(args.pr, utc_compact_timestamp()),
    )
    initialize_input = build_initialize_input(
        root_identity, budgets, initial_route, evaluator_contract_sha256(),
    )
    print("=== graphwing-run-control-initialize ===")
    status, rid, _run, trace = pg.run_slug(mcp, "graphwing-run-control-initialize",
                                            {"input": initialize_input}, wait=args.wait)
    print("run", rid, "->", status)
    summarize(trace)
    if status != "completed":
        return 1
    identity_output = None
    for entry in trace_entries(trace):
        if isinstance(entry, dict) and (entry.get("nodeId") or entry.get("id")) == "identity":
            identity_output = entry.get("output")
            break
    if not isinstance(identity_output, dict) or not identity_output.get("run_control_id"):
        print("initialize did not produce a run_control_id")
        return 1
    run_control_id = identity_output["run_control_id"]
    print("run_control_id", run_control_id)

    payload = build_pr_drive_input(
        args.repo, args.pr, args.test, args.auto_merge, args.klass, args.size,
        args.work_kind, "", args.message, run_control_id,
    )
    print("\n=== graphwing-pr-drive ===")
    print("input", json.dumps(payload))
    status, rid, _run, trace = pg.run_slug(mcp, "graphwing-pr-drive", {"input": payload}, wait=args.wait)
    print("run", rid, "->", status)
    print("nodes:")
    summarize(trace)
    last = 0 if status == "completed" else 1

    why = failed_node(trace)
    if why:
        node, err = why
        print("run stopped at %s: %s" % (node, err[:200]))

    record_workflow_run({
        "workflow": "graphwing-pr-drive", "status": status, "source": "drive-pr",
        "input": payload, "run_id": rid, "run_control_id": run_control_id,
    })

    state = findings(args.repo, args.pr)
    if state is not None:
        print("after run: grade=%s blocking=%s major=%s minor=%s"
              % (state.get("grade"), state.get("blocking"), state.get("major"), state.get("minor")))
    return last


if __name__ == "__main__":
    sys.exit(main())
