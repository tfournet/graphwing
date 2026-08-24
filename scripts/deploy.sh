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

GRAPHWING_HOME=. python3 "$SRC/test_server.py" >/dev/null 2>&1 \
  || { echo "tests are red, not deploying" >&2; exit 1; }

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
