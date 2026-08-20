# graphwing catalog

This git repo is the **node catalog** source of truth. It is not a mega-agent and not the runtime.

- Runtime is `$GRAPHWING_HOME` (zbook: `~/.graphwing`). Secrets, jobs, Hermes state stay there. Do not commit them.
- Graph in org **tim-graphwing** owns topology. graphwing exposes named OpenAPI ops. `agentRun` is one bounded job.
- Prefer a new deterministic op over an agent loop. New workflow JSON only when **edges** change; loop iterations are run `input`.
- Laptop-only: git, local `gh`, file head, units, Herdr, allowlisted scripts/tests. Cloud GitHub/Shortcut stay Rewst integrations.
- `rr` and a named Cloudflare tunnel are optional. Default install is loopback, no `rr`.
- Do not install this on Rewst Internal. Do not ship as a platform package.

## Where to edit

| Thing | Path |
|---|---|
| HTTP ops | `server.py` + `openapi.json` + `test_server.py` |
| Published graphs | `graphs/*.json` then `python3 scripts/publish_graphs.py` |
| Hermes seat text | `SOUL.md` (copied to `$GRAPHWING_HOME` on install) |
| Install / units | `install.py`, `systemd/`, `bin/graphwing` |

Do not copy `AGENTS.md` into `$GRAPHWING_HOME`. `agentRun` cwd is the **target repo** (usually riftwing); that repo's `AGENTS.md` applies there.

## Tests

```bash
GRAPHWING_HOME=. python3 test_server.py
```

Start a Graph run with `{ "input": { ... } }`. `response_webhook_url` is resume only.

## Human surface

Herdr session `graphwing`, tab `graph`. Do not type into the Hermes chat pane.
