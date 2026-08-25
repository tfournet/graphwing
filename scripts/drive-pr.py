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

Needs the Rewst MCP token: GRAPHWING_REWST_MCP_TOKEN, or mcp_bws_key in
rewst-install.json plus BWS_ACCESS_TOKEN.
"""
import argparse
import json
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
    # run_slug posts this body verbatim. It must be wrapped: a bare payload
    # arrives as a manual trigger with body {} and CTX.INPUT is never created,
    # so every {{ CTX.INPUT.* }} in the graph resolves empty and pr-drive dies
    # at ghPrView with no `number`. The smoke run in publish_graphs.py has
    # always wrapped it; this script did not.
    print("input", json.dumps(payload))
    status, rid, run, trace = pg.run_slug(
        mcp, "graphwing-pr-drive", {"input": payload}, wait=args.wait
    )
    print("run", rid, "->", status)
    print("output", json.dumps(run.get("output"))[:600])
    print("nodes:")
    summarize(trace)
    print("full trace: /tmp/gw-run-graphwing-pr-drive.json")
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
