# graphwing catalog

This git repo is the **node catalog** source of truth. It is not a mega-agent and not the runtime.

- Runtime is `$GRAPHWING_HOME` (default `~/.graphwing`). Secrets, jobs, Hermes state stay there. Do not commit them.
- Rewst Graph owns topology. graphwing exposes named OpenAPI ops. `agentRun` is one bounded job.
- Prefer a new deterministic op over an agent loop. New workflow JSON only when **edges** change; loop iterations are run `input`.
- Laptop-only: git, local `gh`, file head, units, Herdr, allowlisted scripts/tests. Cloud GitHub/Shortcut stay Rewst integrations.
- Clean machine: `./start.sh`. Optionally grabs Hermes Agent / herdr / cloudflared. `rr` is a drop-in `rr.json` plugin if you already have the binary; tests stay local (`testRun` / `test_server.py`).
- Do not install this on Rewst Internal. Do not ship as a platform package.

## Where to edit

| Thing | Path |
|---|---|
| HTTP ops | `server.py` + `openapi.json` + `test_server.py` |
| Published graphs | `graphs/*.json` then `scripts/publish_graphs.py` (reads `$GRAPHWING_HOME/rewst-install.json` + public URL from tunnel meta) |
| Hermes seat text | `SOUL.md` (copied to `$GRAPHWING_HOME` with `$GRAPHWING_HOME` substituted) |
| Install / units | `start.sh` (clean machine), `install.py`, `systemd/`, `bin/graphwing` |

Do not copy `AGENTS.md` into `$GRAPHWING_HOME`. `agentRun` cwd is an allowlisted repo short name from `$GRAPHWING_HOME/repos.json` (empty until the wizard / `--repo`). That checkout's `AGENTS.md` applies there.

## Git (tfournet/graphwing)

Work on a branch. Open a PR. Rebase onto current `main` before merge. Merge the PR as part of finishing the task (`gh pr merge --rebase --delete-branch`). Do not wait for Tim to merge. Do not push commits straight to `main`.

## Tests

```bash
GRAPHWING_HOME=. python3 test_server.py
```

Start a Graph run with `{ "input": { ... } }`. `response_webhook_url` is resume only.

## Human surface

Herdr session `graphwing`, tab `graph`. Do not type into the Hermes chat pane.
