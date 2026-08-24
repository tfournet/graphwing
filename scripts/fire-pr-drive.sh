#!/usr/bin/env bash
# Drive a PR to green: scripts/fire-pr-drive.sh <repo> <pr> [test] [auto_merge]
#
# Posts to this host's /v1/rewst/fire, which proxies to the Rewst trigger, so
# the seat never holds the webhook key.
set -euo pipefail

repo="${1:?repo}"; pr="${2:?pr number}"; test_name="${3:-riftwing-local-gates}"
auto_merge="${4:-false}"
HOME_DIR="${GRAPHWING_HOME:-$HOME/.graphwing}"
KEY="$(cat "$HOME_DIR/api.key")"

payload=$(python3 -c '
import json, sys
print(json.dumps({"workflow": "pr-drive", "input": {
    "repo": sys.argv[1], "pr_number": sys.argv[2], "test": sys.argv[3],
    "attempt": 1, "max_attempts": 3,
    "auto_merge": sys.argv[4].lower() == "true",
}}))' "$repo" "$pr" "$test_name" "$auto_merge")

curl -sS -m 30 -X POST -H "X-Graphwing-Key: $KEY" -H 'Content-Type: application/json' \
  -d "$payload" "http://127.0.0.1:8645/v1/rewst/fire"
echo
