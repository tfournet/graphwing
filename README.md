# graphwing

Local node catalog for Rewst Graph.

Source of truth: [`tfournet/graphwing`](https://github.com/tfournet/graphwing). Runtime lives in `$GRAPHWING_HOME` (default `~/.graphwing`): API key, `auth.json`, tunnel token, jobs, Hermes state. Those stay out of git.

## Install

On a machine that is not this laptop:

```bash
git clone https://github.com/tfournet/graphwing.git
cd graphwing
python3 install.py
```

`install.py` copies catalog files into `$GRAPHWING_HOME`, writes user systemd units, and generates an API key. It does not copy secrets from another home.

Non-interactive (no TTY, tests, or first-cut):

```bash
python3 install.py --non-interactive --tunnel none
```

- **API key:** `$GRAPHWING_HOME/api.key` (mode 600) or `export GRAPHWING_KEY=...`. Send header `X-Graphwing-Key`.
- **Named-tunnel credentials:** `$GRAPHWING_HOME/cloudflared.token`, or `$GRAPHWING_HOME/tunnel.env` / `GRAPHWING_CF_API_KEY` for `setup_tunnel.py`. See `examples/tunnel.env.example`.
- **`repos.json` / `stacks.json` / `rr.json`:** local config under `$GRAPHWING_HOME`, not compiled into `server.py`. The copies in this repo are the zbook test checkout; install writes machine-local files instead of copying them.
- **`rr`:** optional. Skip the wizard step or omit `rr.json`. `POST /v1/rr/run` is `501 not_configured` when unset.
- **Tunnel:** default is none (loopback `127.0.0.1:8645`). `demo` is a Cloudflare quick tunnel (`*.trycloudflare.com`, hostname rotates — fine for a short Rewst demo, not a saved integration). `named` is opt-in. Rewst Graph SSRF-blocks loopback, RFC1918, and Tailscale, so a stable public hostname is still required for a saved cloud Graph integration.

```bash
GRAPHWING_HOME=. python3 test_server.py
```

## zbook seat (example)

Runtime on zbook is `~/.graphwing`. Org wiring below is this laptop's Rewst seat, not part of install.

- Org: https://app.rewst.ai/orgs/tim-graphwing
- Public API: https://graphwing.tfour.net
- Human: `herdr --session graphwing`
- Graph activity: Herdr tab `graph` (one line per op). Long jobs (`agentRun`, async `scriptRun`) also get a `gw-*` tab that tails the log. Does not type into the Hermes chat pane.
- CLI: `graphwing` (`HERMES_HOME=$GRAPHWING_HOME`, zbook example: `/home/tim/.graphwing`)

Graph uses named OpenAPI actions, not a mega-agent. Prefer a new deterministic op over an agent loop.

`POST /v1/agent/run` queues one bounded Hermes loop (`HERMES_HOME=$GRAPHWING_HOME`, max 30 turns / 300s) and returns 202 + `job_id`. Graph join: `action.wait.webhook` pending → pass `response_webhook_url` (`resumeUrl`) and `response_webhook_token` (`resumeToken`); graphwing POSTs the receipt with `X-Rewst-Token`. Poll `GET /v1/agent/jobs/{job_id}` is optional. v0.4 seat is `graphwing` home only (`profiles.json` allowlist; `~/.hermes/profiles` is not dumped).

Git writes are opt-in Graph ops on allowlisted repo short names: `gitCheckout`, `gitCommit`, `gitPush`. No `--force`, no `git add -A` unless staged/add paths are explicit. `POST /v1/script/run` runs `scripts.json` names only. Deterministic stack ops: `stackStatus`, `portCheck`, `testRun` (`tests.json`), `rrRun` (`rr.json` recipes only — no free-form `rr run`). A Rewst PR-driver is a Graph sub-workflow of these nodes (`gitStatus` → `ghPrChecks` → `testRun` / `rrRun` → optional `agentRun` + wait-webhook), not one agent loop.

Laptop-only here: git, local `gh`, file head, units, Herdr, allowlisted scripts, one-job agent loops. Cloud GitHub/Shortcut stay Rewst integrations — do not duplicate them on this host.

## Units (user systemd)

Templates in `systemd/` have no hard-coded home. `install.py` renders them into `~/.config/systemd/user/`.

- `graphwing-api` — 127.0.0.1:8645 (always)
- `graphwing-tunnel` — only if you picked demo or named
- `graphwing-herdr` — only if `herdr` is on PATH

zbook example: named tunnel `zbook-graphwing` → graphwing.tfour.net.

API key: `$GRAPHWING_HOME/api.key` (header `X-Graphwing-Key`). Health and `/openapi.json` are unauthenticated.

## Rewst (org tim-graphwing only)

- Org: https://app.rewst.ai/orgs/tim-graphwing
- MCP token BWS key: `app_rewst_ai__tim_graphwing_mcp_token`
- Custom integration `graphwing` imported from `https://graphwing.tfour.net/openapi.json` (spec 0.5.0)
- Canary workflow: `graphwing-canary` (manual → health → gitStatus)
- Agent canary: `graphwing-agentrun-canary` (manual → wait.webhook pending → agentRun → resume receipt)
- `graphwing-verify-stack` — payload `{input:{stack,ports}}` → stackStatus → portCheck
- `graphwing-implement-slice` — payload `{input:{repo,prompt}}` → gitStatus → wait.webhook → agentRun → receipt
- `graphwing-pr-drive` — payload `{input:{repo,pr,test,prompt}}`. `gitStatus` → `ghPrView` → `ghPrChecks`. Green → done. Red → named `testRun` (sync recipe) → one implement-slice (`wait` + `agentRun`) → checks again. Missing PR / pending / test fail / still red → Herdr noop. Specs in `graphs/`.
- Start a run: `POST /api/workflows/{slug}/run` with `{ "input": { ... } }` (that JSON is the start webhook body). Resume is a different POST to `response_webhook_url`.
- `ghPrChecks` HTTP 200 means `gh` ran. Graph gates on `data.all_green` / `data.any_red`, not an agent reading the transcript.

Do not install this on Rewst Internal. Do not ship as a platform package.
