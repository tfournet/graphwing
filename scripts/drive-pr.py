#!/usr/bin/env python3
"""Drive a PR to green through graphwing-pr-drive, and print the node trace.

Every attempt is now run-control reservation-backed and the retry loop lives
inside the graph (graphs/pr-drive.json's capped `attempts` logic.loop), not
here. This script submits operator intent for an existing Rewst-owned durable run and
reads back the bounded graph result. It does not select routes or create aggregate limits.

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
