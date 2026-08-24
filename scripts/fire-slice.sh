#!/usr/bin/env bash
# Fire graphwing-implement-slice at its Rewst webhook trigger.
#
#   scripts/fire-slice.sh payload.json
#   scripts/fire-slice.sh -            # read the payload from stdin
#
# Secrets come from $GRAPHWING_HOME/.env (mode 600, never committed):
#   GRAPHWING_HOOK_SECRET               matches $GRAPHWING_HOOK_SECRET in graphs/*.json
#   GRAPHWING_IMPLEMENT_SLICE_HOOK_URL  recorded after publish_graphs.py
#
# The payload is the workflow input, e.g.
#   { "input": { "repo": "riftwing", "branch": "...", "index": "...", ... } }

set -euo pipefail

HOME_DIR="${GRAPHWING_HOME:-$HOME/.graphwing}"
ENV_FILE="$HOME_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "no $ENV_FILE. Copy the hook secret from rewst-install.json into it." >&2
    exit 1
fi
# shellcheck disable=SC1090 # path is resolved at runtime from GRAPHWING_HOME
. "$ENV_FILE"

: "${GRAPHWING_HOOK_SECRET:?set GRAPHWING_HOOK_SECRET in $ENV_FILE}"
if [ -z "${GRAPHWING_IMPLEMENT_SLICE_HOOK_URL:-}" ]; then
    echo "GRAPHWING_IMPLEMENT_SLICE_HOOK_URL is empty in $ENV_FILE." >&2
    echo "Run scripts/publish_graphs.py to (re)publish implement-slice, then record" >&2
    echo "the trigger id it returns. Shape:" >&2
    echo "  https://app.rewst.ai/api/hooks/\$GRAPHWING_ORG_ID/trigger/<trigger_id>" >&2
    exit 1
fi

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

# Fail before the POST rather than letting Rewst reject malformed JSON.
printf '%s' "$PAYLOAD" | python3 -c 'import json,sys; json.load(sys.stdin)' || {
    echo "payload is not valid JSON" >&2
    exit 1
}

echo "POST $GRAPHWING_IMPLEMENT_SLICE_HOOK_URL" >&2
code="$(printf '%s' "$PAYLOAD" | curl -sS -o /tmp/fire-slice.out -w '%{http_code}' \
    -X POST "$GRAPHWING_IMPLEMENT_SLICE_HOOK_URL" \
    -H "x-rewst-secret: $GRAPHWING_HOOK_SECRET" \
    -H 'Content-Type: application/json' \
    --data-binary @-)"
echo "HTTP $code" >&2
cat /tmp/fire-slice.out
echo
case "$code" in
    2*) echo "fired. watch tab graph." >&2 ;;
    401|403) echo "rejected: x-rewst-secret does not match the trigger. Republish or fix .env." >&2; exit 1 ;;
    *) echo "unexpected status" >&2; exit 1 ;;
esac
