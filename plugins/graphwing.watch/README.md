# Graphwing

One bar icon for the local graphwing daemon. The panel is a display: it polls
`GET /v1/watch` on `127.0.0.1` and draws units plus in-flight and recent jobs.

Install copies this folder to `~/.config/omarchy/plugins/graphwing.watch/`.
`install.py` does that when Omarchy is present. Or copy it yourself and run:

```bash
omarchy plugin validate ~/.config/omarchy/plugins/graphwing.watch
omarchy plugin enable graphwing.watch --section right --before omarchy.agents
```

## Panel

- **Hero** — Graphwing, and a one-line status (`Idle`, `2 running`, `Daemon down`).
- **Units** — `api`, `tunnel`, `herdr` as systemd --user reports them. Herdr is
  optional. The bar only goes red when `graphwing-api` is down.
- **Jobs** — queued and running first, then the last eight finished. Failed
  rows use the urgent color. Titles prefer a ticket path or PR number, then a
  heading slug like `01-login`, then the recipe or reviewer. No webhook tokens,
  no transcripts.

The icon sits dim when the daemon is unreachable, lights when jobs are in
flight, and uses the urgent color when the API is down or a recent job failed.
A small count appears on the icon while anything is queued or running.

## Data

`status.py` reads `$GRAPHWING_HOME/api.key` (default `~/.graphwing/api.key`) and
GETs `http://127.0.0.1:<port>/v1/watch`. The key stays off the process list.
The daemon must be the copy in `$GRAPHWING_HOME` that already serves `/v1/watch`;
`scripts/deploy.sh` copies catalog `server.py` there.

## Interactions

- Bar icon: left = panel, right = refresh, middle = open herdr session `graphwing`.
- Panel: `r` refresh, Enter on the hero opens herdr, arrows/`j`/`k` move the
  job list, Esc closes.
- IPC: `omarchy-shell graphwing.watch <open|close|toggle|refresh|status|herdr|journal>`.

## Settings

| Key | Default | What it does |
|---|---|---|
| `refreshIntervalSec` | `5` | How often the helper hits `/v1/watch` |
| `port` | `8645` | Daemon listen port |
| `home` | `~/.graphwing` | Seat directory that holds `api.key` |

```bash
omarchy bar set graphwing.watch refreshIntervalSec 10 --json
omarchy bar set graphwing.watch home ~/.graphwing
```
