#!/usr/bin/env bash
# Fire graphwing-implement-slice.
#
#   scripts/fire-slice.sh payload.json
#   scripts/fire-slice.sh -            # read the payload from stdin
#
# Posts to this host's own /v1/rewst/fire, which proxies to the Rewst webhook.
# The trigger URL and the shared secret live in $GRAPHWING_HOME/rewst-install.json
# and never leave the service, so this script only needs the local API key.
#
# The payload is the workflow input:
#   { "input": { "repo": "riftwing", "branch": "...", "index": "...", ... } }

set -euo pipefail

HOME_DIR="${GRAPHWING_HOME:-$HOME/.graphwing}"
BASE="${GRAPHWING_URL:-http://127.0.0.1:8645}"
KEY_FILE="$HOME_DIR/api.key"
WORKFLOW="${GRAPHWING_WORKFLOW:-implement-slice}"

if [ ! -f "$KEY_FILE" ]; then
    echo "no $KEY_FILE. Is graphwing installed?" >&2
    exit 1
fi
KEY="${GRAPHWING_KEY:-$(cat "$KEY_FILE")}"

PAYLOAD_FILE="${1:-}"
if [ -z "$PAYLOAD_FILE" ]; then
    echo "usage: $0 <payload.json|->" >&2
    exit 1
fi
if [ "$PAYLOAD_FILE" = "-" ]; then
    PAYLOAD="$(cat)"
else
    PAYLOAD="$(cat "$PAYLOAD_FILE")"
fi

# Unwrap {"input": {...}} so the file stays the same shape a manual run takes,
# and fail here rather than letting Rewst reject malformed JSON.
BODY="$(printf '%s' "$PAYLOAD" | WORKFLOW="$WORKFLOW" python3 -c '
import json, os, sys
doc = json.load(sys.stdin)
inp = doc.get("input", doc)
if not isinstance(inp, dict):
    sys.exit("payload input must be an object")
json.dump({"workflow": os.environ["WORKFLOW"], "input": inp}, sys.stdout)
')"

echo "POST $BASE/v1/rewst/fire ($WORKFLOW)" >&2
code="$(printf '%s' "$BODY" | curl -sS -o /tmp/fire-slice.out -w '%{http_code}' \
    -X POST "$BASE/v1/rewst/fire" \
    -H "X-Graphwing-Key: $KEY" \
    -H 'Content-Type: application/json' \
    --data-binary @-)"
echo "HTTP $code" >&2
cat /tmp/fire-slice.out
echo
case "$code" in
    2*) echo "fired. watch tab graph." >&2 ;;
    401) echo "rejected: local API key is wrong. Check $KEY_FILE." >&2; exit 1 ;;
    503) echo "graphwing has no trigger url or secret recorded. See the body above." >&2; exit 1 ;;
    502) echo "graphwing reached Rewst and Rewst refused. See the body above." >&2; exit 1 ;;
    *) echo "unexpected status" >&2; exit 1 ;;
esac
