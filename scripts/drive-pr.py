#!/usr/bin/env python3
"""Drive a PR to green through graphwing-pr-drive, and print the node trace.

Every writer is run-control reservation-backed. One bounded graph invocation
can launch at most one writer; later invocations resume the same Rewst-owned
durable run. This script submits operator intent and reads back the bounded
graph result. It does not select routes, create aggregate limits, or poll for
review policy.

Why REST and not the webhook: a webhook-triggered run leaves CTX.INPUT empty,
so every {{ CTX.INPUT.* }} in the graph resolves to nothing and pr-drive dies at
ghPrView with no `number` on the query string. That failure is invisible from
outside — `repo` still arrives, because openapi.json declares a default of
"riftwing" for it, so the request looks half-right. POST /workflows/{slug}/run
passes inputs properly; it is how implement-slice has always been fired.

  python3 scripts/drive-pr.py 3526 [--repo riftwing] [--test riftwing-local-gates]
                                   [--auto-merge] [--wait 900]

Budget it end to end before wrapping this in anything with its own timeout.
--wait defaults to the graph's longest reachable wait path, derived from the
graph rather than guessed (see worst_case_run_seconds). After a successful
push the workflow returns `await_exact_head_audit`; a later invocation resumes
from durable history after exact-head audit evidence exists.

Needs the Rewst MCP token: GRAPHWING_REWST_MCP_TOKEN, or mcp_bws_key in
rewst-install.json plus BWS_ACCESS_TOKEN.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_graphs as pg  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import resolve_executable  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DAEMON_BASE = "http://127.0.0.1:8645"


# --------------------------------------------------------------------------
# Pure payload builders, pinned by DrivePrScriptTests without a daemon, a
# repo, or a Rewst token.
# --------------------------------------------------------------------------

def build_pr_drive_input(repo: str, pr: str, test: str, auto_merge: bool,
                          klass: str, size: str, work_kind: str, prompt: str,
                          commit_message: str, run_control_id: str) -> dict[str, Any]:
    """graphwing-pr-drive input for an existing durable v2 run."""
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
    The source graph is a DAG, so the longest reachable path is finite. Node
    weights are webhook wait timeouts; all other local and GitHub work shares
    a fixed orchestration margin.
    """
    graph = json.loads((ROOT / "graphs" / "pr-drive.json").read_text())["spec"]
    nodes = {n["id"]: n for n in graph["nodes"]}
    adjacency: dict[str, list[str]] = {}
    for edge in graph["edges"]:
        adjacency.setdefault(edge["source"], []).append(edge["target"])

    memo: dict[str, int] = {}
    active: set[str] = set()

    def longest(node_id: str) -> int:
        if node_id in memo:
            return memo[node_id]
        if node_id in active:
            raise ValueError("pr-drive must remain acyclic")
        active.add(node_id)
        node = nodes[node_id]
        own = (int(node["config"]["timeoutSeconds"])
               if node["type"] == "action.wait.webhook" else 0)
        downstream = max((longest(target) for target in adjacency.get(node_id, [])), default=0)
        active.remove(node_id)
        memo[node_id] = own + downstream
        return memo[node_id]

    return 300 + longest("join_start")


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
    ap.add_argument("--run-control-id", required=True,
                     help="existing v2 durable run selected by the owning Rewst lifecycle")
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

    print("=== reading the PR head ===")
    repos = load_repos()
    repo_path = resolve_repo_path(args.repo, repos)
    branch, head_sha = gh_pr_view(repo_path, args.pr)
    print("branch", branch, "head_sha", head_sha)

    install = pg.load_install()
    pg.TENANT = pg.tenant_id(install)
    mcp = pg.rewst_mcp(install)

    run_control_id = args.run_control_id
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


    state = findings(args.repo, args.pr)
    if state is not None:
        print("after run: grade=%s blocking=%s major=%s minor=%s"
              % (state.get("grade"), state.get("blocking"), state.get("major"), state.get("minor")))
    return last


if __name__ == "__main__":
    sys.exit(main())
