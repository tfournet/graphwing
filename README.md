# graphwing

Local node catalog for Rewst Graph.

Source of truth: [`tfournet/graphwing`](https://github.com/tfournet/graphwing). Runtime lives in `$GRAPHWING_HOME` (default `~/.graphwing`): API key, `auth.json`, tunnel token, jobs, Hermes state. Those stay out of git.

## Install

```bash
git clone https://github.com/tfournet/graphwing.git
cd graphwing
./start.sh
```

`start.sh` optionally installs Hermes Agent (`agentRun`), herdr, and cloudflared, writes `$GRAPHWING_HOME`, and starts the API in this terminal. It does not copy secrets from another home. It does not install `rr`.

```bash
curl -fsSL https://raw.githubusercontent.com/tfournet/graphwing/main/start.sh | bash -s -- --yes
./start.sh --yes --with-hermes
./start.sh --yes --no-start
./start.sh --daemon
```

`install.py` is the catalog renderer `start.sh` calls.

- **API key:** `$GRAPHWING_HOME/api.key` (mode 600) or `export GRAPHWING_KEY=...`. Header `X-Graphwing-Key`.
- **`repos.json`:** empty until the wizard or `install.py --repo name=/path`. See `examples/repos.example.json`.
- **`rr`:** drop-in plugin. Copy `examples/rr.example.json` to `$GRAPHWING_HOME/rr.json` if you already have `rr`. Otherwise `POST /v1/rr/run` is `501 not_configured`. Tests stay local (`python3 test_server.py`).
- **Tunnel:** default none (loopback `127.0.0.1:8645`). `demo` is an ephemeral `*.trycloudflare.com` URL (journal). `named` runs `setup_tunnel.py` when `tunnel.env` or `GRAPHWING_CF_API_KEY` is present; that writes `$GRAPHWING_HOME/cloudflared-meta.json` and sets the OpenAPI `servers` URL to `https://<hostname>`. Rewst Graph SSRF-blocks loopback, so a stable public hostname is required for a saved cloud integration.

```bash
GRAPHWING_HOME=. python3 test_server.py
```

## Runtime

- API: `127.0.0.1:8645` (health and `/openapi.json` unauthenticated)
- Human: `herdr --session graphwing`, tab `graph`
- CLI: `graphwing` (`HERMES_HOME=$GRAPHWING_HOME`)
- Units: `graphwing-api` always; `graphwing-tunnel` / `graphwing-herdr` only if you opted in

Graph uses named OpenAPI actions, not a mega-agent. Prefer a new deterministic op over an agent loop.

`POST /v1/doorbell/pr-drive` accepts a GitHub Actions OIDC bearer token, maps the verified repository and actor through `$GRAPHWING_HOME/doorbell.json` (see `examples/doorbell.example.json`), and rings the secret Rewst pr-drive hook configured in `$GRAPHWING_HOME/rewst-install.json`; it does not accept `X-Graphwing-Key` as authorization.

The GitHub Action reads `<!-- pr-drive-hook: URL -->` from the PR body and POSTs `{pr}` to that Rewst webhook, so no GitHub secret is required. The status graph webhook has no hook secret; the fix graph (`pr-drive`) remains authenticated with `x-rewst-secret`.

`POST /v1/agent/run` queues one bounded Hermes loop (`HERMES_HOME=$GRAPHWING_HOME`, max 30 turns / 300s) and returns 202 + `job_id`. Spawn sets `TERMINAL_CWD` / `--in` to the allowlisted repo cwd. Graph join: `action.wait.webhook` pending → `response_webhook_url` / `response_webhook_token`; graphwing POSTs the receipt with `X-Rewst-Token`.

Git writes are opt-in on allowlisted short names: `gitCheckout`, `gitRestore`, `gitCommit`, `gitPush`. No `--force`, no `git add -A` unless paths are explicit. `scriptRun` / `testRun` are allowlisted names only.

## Rewst

Import a custom integration from the seat OpenAPI URL (`GET /openapi.json` `servers[0].url`, or `GRAPHWING_PUBLIC_URL`). Named-tunnel setup is what fills that URL.

Publish graphs from this repo:

```bash
# $GRAPHWING_HOME/rewst-install.json from examples/rewst-install.example.json
GRAPHWING_REWST_MCP_TOKEN=... python3 scripts/publish_graphs.py --only pr-drive
```

Do not install this on Rewst Internal. Do not ship as a platform package.
