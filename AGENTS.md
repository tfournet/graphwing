# graphwing catalog

This git repo is the **node catalog** source of truth. It is not a mega-agent and not the runtime.

- Runtime is `$GRAPHWING_HOME` (default `~/.graphwing`). Secrets, jobs, and local runtime state stay there. Do not commit them.
- Rewst Graph owns topology. graphwing exposes named OpenAPI ops. `agentRun` is one bounded job.
- Prefer a new deterministic op over an agent loop. New workflow JSON only when **edges** change; loop iterations are run `input`.
- This host only: git, local `gh`, file head, units, Herdr, allowlisted scripts/tests, and preinstalled native model launchers. Cloud integrations stay on Rewst Graph.
- Clean machine: `./start.sh`. It can optionally install herdr and cloudflared, but model launchers must already be installed. `rr` is a drop-in `rr.json` plugin; tests stay local (`testRun` / `test_server.py`).
- Do not install this on Rewst Internal. Do not ship as a platform package.

## Where to edit

| Thing | Path |
|---|---|
| HTTP ops | `server.py` + `openapi.json` + `test_server.py` |
| Published graphs | `graphs/*.json` then `scripts/publish_graphs.py` (reads `$GRAPHWING_HOME/rewst-install.json` + public URL from tunnel meta) |
| Human loop | `docs/HUMAN-LOOP.md` — grill → spec → Graph → proof → PR. First run: `docs/USING.md`. Do not duplicate them here. |
| Install / units | `start.sh` (clean machine), `install.py`, `systemd/` |
| Omarchy bar | `~/work/graphwing.watch` (own repo). `install.py` here runs that checkout's installer. |

Do not copy `AGENTS.md` into `$GRAPHWING_HOME`. `agentRun` cwd is an allowlisted repo short name from `$GRAPHWING_HOME/repos.json` (empty until the wizard / `--repo`). That checkout's `AGENTS.md` applies there.

## Git (tfournet/graphwing)

Work in a git worktree (`/home/tim/work/gw-<short>`), not the primary clone. Branch from `origin/main`. Open a PR. Rebase onto current `main` before merge. Merge the PR as part of finishing the task (`gh pr merge --rebase --delete-branch`). Do not wait for Tim to merge. Do not push commits straight to `main`. After merge, `git worktree remove` the tree. If merge cannot check out `main` because another worktree holds it, still merge the PR and skip the local checkout.

## Tests

```bash
GRAPHWING_HOME=. python3 test_server.py
```

Start a Graph run with `{ "input": { ... } }`. `response_webhook_url` is resume only.

## Human surface

Herdr session `graphwing` and tab `graph` remain an optional deterministic job dashboard. Do not chat there. Use your normal editor or terminal for operator work. Operator lock: `docs/HUMAN-LOOP.md`.
