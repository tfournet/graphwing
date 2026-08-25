#!/usr/bin/env bash
# Copy catalog code into the seat and restart the API.
#
# systemd runs $GRAPHWING_HOME/server.py, which is a copy, not a symlink. A bare
# `systemctl restart` therefore reports success while still serving whatever was
# last copied across, and nothing anywhere says the new code is not live.
#
# repos.json and stacks.json are tracked in git but hold placeholders; the real
# ones are seat config. Never copy them.
set -euo pipefail

HOME_DIR="${GRAPHWING_HOME:-$HOME/.graphwing}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FILES=(server.py openapi.json setup_tunnel.py scripts.json tests.json)

[ -d "$HOME_DIR" ] || { echo "no seat home: $HOME_DIR" >&2; exit 1; }

# This copies whatever the working tree holds, so it deploys whatever branch is
# checked out. Another session running `git checkout` in a shared clone silently
# ships older code with a success message, which is exactly how a published
# pr-drive graph ended up calling an endpoint the live server no longer had.
# Work from a worktree on the branch you mean; override deliberately if not.
BRANCH="$(git -C "$SRC" rev-parse --abbrev-ref HEAD)"
WANT="${GRAPHWING_DEPLOY_BRANCH:-main}"
if [ "$BRANCH" != "$WANT" ]; then
  echo "refusing to deploy: $SRC is on '$BRANCH', expected '$WANT'" >&2
  echo "use a worktree on $WANT, or set GRAPHWING_DEPLOY_BRANCH=$BRANCH" >&2
  exit 1
fi
if ! git -C "$SRC" diff --quiet || ! git -C "$SRC" diff --cached --quiet; then
  echo "note: deploying uncommitted changes from $SRC" >&2
fi
echo "deploying $BRANCH from $SRC"

GRAPHWING_HOME=. python3 "$SRC/test_server.py" >/dev/null 2>&1 \
  || { echo "tests are red, not deploying" >&2; exit 1; }

# Deploying restarts graphwing-api, which drops every in-flight wait webhook and
# kills any running writer. A run that has already spent five minutes on an
# agent session then dies for no reason visible in its trace.
# grep -l exits 1 when nothing matches and xargs propagates it, so under
# `set -e` this whole pipeline killed the script with no message at all: no
# refusal, no copy, and a clean-looking exit. Swallow it explicitly.
RUNNING="$(find "$HOME_DIR/jobs" -maxdepth 2 -name job.json -newermt '-30 minutes' 2>/dev/null \
  | xargs -r grep -l '"status": *"running"' 2>/dev/null | wc -l || true)"
if [ "${RUNNING:-0}" -gt 0 ] && [ -z "${GRAPHWING_DEPLOY_ANYWAY:-}" ]; then
  echo "refusing: $RUNNING job(s) still running; a restart would kill them" >&2
  echo "wait, or set GRAPHWING_DEPLOY_ANYWAY=1 to override" >&2
  exit 1
fi

for f in "${FILES[@]}"; do
  if cmp -s "$SRC/$f" "$HOME_DIR/$f"; then
    echo "  same  $f"
  else
    install -m "$(stat -c '%a' "$SRC/$f")" "$SRC/$f" "$HOME_DIR/$f"
    echo "  COPY  $f"
  fi
done

systemctl --user restart graphwing-api
sleep 2
systemctl --user is-active --quiet graphwing-api || { echo "service did not come back" >&2; exit 1; }

# Prove the live file is the one we just built, not a green restart of stale code.
for f in "${FILES[@]}"; do
  cmp -s "$SRC/$f" "$HOME_DIR/$f" || { echo "still stale after deploy: $f" >&2; exit 1; }
done
echo "deployed and serving: $(git -C "$SRC" rev-parse --short HEAD)"
