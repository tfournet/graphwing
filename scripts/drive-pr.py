#!/usr/bin/env python3
"""Drive a PR to green through graphwing-pr-drive, and print the node trace.

Why REST and not the webhook: a webhook-triggered run leaves CTX.INPUT empty,
so every {{ CTX.INPUT.* }} in the graph resolves to nothing and pr-drive dies at
ghPrView with no `number` on the query string. That failure is invisible from
outside — `repo` still arrives, because openapi.json declares a default of
"riftwing" for it, so the request looks half-right. POST /workflows/{slug}/run
passes inputs properly; it is how implement-slice has always been fired.

  python3 scripts/drive-pr.py 3526 [--repo riftwing] [--test riftwing-local-gates]
                                   [--auto-merge] [--wait 900]

Budget it end to end before wrapping this in anything with its own timeout. One
attempt is a graph run (up to --wait) plus a review wait (up to
GRAPHWING_REVIEW_WAIT, default 2700s because a full review can take 30 minutes).
Three attempts is therefore on the order of three hours. Every timeout in the
chain has to clear that: an outer wrapper that expires mid-loop kills the run
after the writer has already done the work.

Needs the Rewst MCP token: GRAPHWING_REWST_MCP_TOKEN, or mcp_bws_key in
rewst-install.json plus BWS_ACCESS_TOKEN.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_graphs as pg  # noqa: E402


def summarize(trace) -> None:
    """One line per node: what ran, in order, and how it ended."""
    if isinstance(trace, dict):
        tasks = trace.get("trace") or trace.get("tasks")
    else:
        tasks = trace
    if not isinstance(tasks, list):
        print("trace shape unexpected:", json.dumps(trace)[:400])
        return
    for t in tasks:
        if not isinstance(t, dict):
            continue
        name = t.get("id") or t.get("nodeId") or t.get("label") or "?"
        status = t.get("status") or t.get("state") or "?"
        err = t.get("error") or ""
        line = "  %-24s %s" % (name, status)
        if err:
            line += "  " + str(err)[:160]
        print(line)


def findings(mcp: str, repo: str, pr: str):
    """Read the PR's machine verdict through the same endpoint the graph uses."""
    import urllib.error
    import urllib.request
    home = Path(os.environ.get("GRAPHWING_HOME", str(Path.home() / ".graphwing")))
    key = (home / "api.key").read_text().strip()
    url = "http://127.0.0.1:8645/v1/gh/pr/findings?repo=%s&number=%s" % (repo, pr)
    req = urllib.request.Request(url, headers={"X-Graphwing-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, ValueError) as exc:
        print("findings read failed:", exc)
        return None


# A full review can take 30 minutes. Waiting less than that does not fail
# loudly: the driver reads the previous round's findings, hands the writer work
# it already did, and burns an attempt. Budget past the worst case, not the
# typical one.
REVIEW_WAIT_SECONDS = int(os.environ.get("GRAPHWING_REVIEW_WAIT", "2700"))
REVIEW_POLL_SECONDS = 30


def wait_for_fresh_review(mcp, repo, pr, previous, tries: int = 0, gap: int = 0) -> bool:
    """Block until the audit has produced a verdict for the new commit.

    Fingerprints are stable per defect, so a changed set means the reviewer has
    looked again. A returning hold:pm-review means it is still looking.

    Returns True if a fresh verdict arrived, False on timeout, so the caller
    can stop rather than act on a stale read.
    """
    gap = gap or REVIEW_POLL_SECONDS
    tries = tries or max(1, REVIEW_WAIT_SECONDS // gap)
    import time
    before = {f.get("fingerprint") for f in (previous.get("findings") or [])}
    for _ in range(tries):
        time.sleep(gap)
        now = findings(mcp, repo, pr)
        if now is None:
            return False
        if now.get("holds"):
            continue
        after = {f.get("fingerprint") for f in (now.get("findings") or [])}
        if after != before or not now.get("blocking"):
            print("review re-ran: grade=%s blocking=%s" % (now.get("grade"), now.get("blocking")))
            return True
    print("review did not settle within %ds; stopping rather than acting on a stale read"
          % REVIEW_WAIT_SECONDS)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pr")
    ap.add_argument("--repo", default="riftwing")
    ap.add_argument("--test", default="riftwing-local-gates")
    ap.add_argument("--auto-merge", action="store_true")
    ap.add_argument("--max-attempts", default="3")
    ap.add_argument("--wait", type=int, default=900)
    ap.add_argument("--message", default="fix: address review findings",
                    help="commit message; git_commit rejects an empty one")
    args = ap.parse_args()

    install = pg.load_install()
    pg.TENANT = pg.tenant_id(install)
    mcp = pg.rewst_mcp(install)

    payload = {
        "repo": args.repo,
        "pr_number": str(args.pr),
        "test": args.test,
        "attempt": "1",
        "max_attempts": str(args.max_attempts),
        "auto_merge": "true" if args.auto_merge else "false",
        "commit_message": args.message,
    }
    # The loop lives here, not in the graph. The graph's `continue` node kicks
    # the next attempt at kick_url, which is a Rewst webhook, and a
    # webhook-triggered run never creates CTX.INPUT: every template resolves
    # empty and the run dies at ghPrView while Rewst reports it completed. So
    # the chain can only be driven from a caller that can reach
    # POST /workflows/{slug}/run. implement-slice chains slices the same broken
    # way; its one green run was a single-ticket map, so it never noticed.
    last = 1
    for attempt in range(1, int(args.max_attempts) + 1):
        payload["attempt"] = str(attempt)
        print("\n=== attempt %d of %s ===" % (attempt, args.max_attempts))
        # run_slug posts this body verbatim, and it must be wrapped: a bare
        # payload arrives as a manual trigger with body {}.
        print("input", json.dumps(payload))
        status, rid, run, trace = pg.run_slug(
            mcp, "graphwing-pr-drive", {"input": payload}, wait=args.wait
        )
        print("run", rid, "->", status)
        print("nodes:")
        summarize(trace)
        last = 0 if status == "completed" else 1

        state = findings(mcp, args.repo, args.pr)
        if state is None:
            print("could not read findings; stopping")
            return 1
        print("after attempt %d: grade=%s blocking=%s major=%s minor=%s"
              % (attempt, state.get("grade"), state.get("blocking"),
                 state.get("major"), state.get("minor")))
        if not state.get("blocking"):
            print("nothing left to fix")
            return last
        if attempt == int(args.max_attempts):
            print("attempts exhausted with findings still open")
            return 1
        # The audit re-runs against the new sha. Reading findings before it
        # finishes returns the previous round and burns an attempt fixing what
        # is already fixed.
        print("waiting up to %ds for the review to re-run against the new commit..."
              % REVIEW_WAIT_SECONDS)
        if not wait_for_fresh_review(mcp, args.repo, args.pr, state):
            return 1
    return last


if __name__ == "__main__":
    raise SystemExit(main())
