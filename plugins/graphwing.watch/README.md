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
- **Jobs** — in-flight local nodes (agent, test, review) while something is
  running.
- **Recent** — Rewst graphs this seat fired (`implement-slice`, `pr-drive`)
  plus doorbell rings. Ticket or PR in the title. Not the inner agent/test
  jobs. Click focuses herdr tab `graph`.

The icon stays at full contrast while the API is up, so it does not vanish
next to Tailscale when idle. Urgent means the API is down or a job just
failed. A small count appears while anything is queued or running. A new
failure also fires an Omarchy notification. Existing failures at startup
do not.

## Data

`status.py` reads `$GRAPHWING_HOME/api.key` (default `~/.graphwing/api.key`) and
GETs `http://127.0.0.1:<port>/v1/watch`. The key stays off the process list.
The daemon must be the copy in `$GRAPHWING_HOME` that already serves `/v1/watch`;
`scripts/deploy.sh` copies catalog `server.py` there.

## Interactions

- Bar icon: left = panel, right = refresh, middle = open herdr session `graphwing`.
- Panel: `r` refresh, Enter on the hero opens herdr, Enter or click on a job
  focuses its `gw-*` tab (or tab `graph` if that tab is gone), arrows/`j`/`k`
  move the job list, Esc closes.
- IPC: `omarchy-shell graphwing.watch <open|close|toggle|refresh|status|herdr|journal>`.

## Settings

| Key | Default | What it does |
|---|---|---|
| `refreshIntervalSec` | `5` | How often the helper hits `/v1/watch` |
| `port` | `8645` | Daemon listen port |
| `home` | `~/.graphwing` | Seat directory that holds `api.key` |
| `notifyOnFail` | `true` | Desktop notification when a job newly fails |

```bash
omarchy bar set graphwing.watch refreshIntervalSec 10 --json
omarchy bar set graphwing.watch home ~/.graphwing
omarchy bar set graphwing.watch notifyOnFail false --json
```
