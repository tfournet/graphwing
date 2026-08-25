#!/usr/bin/env bash
# Superseded. Use: python3 scripts/drive-pr.py <pr> --message "..."
#
# This posted to the Rewst webhook, which cannot start pr-drive. A
# webhook-triggered run never creates CTX.INPUT, so every {{ CTX.INPUT.* }} in
# the graph resolves empty and the run dies at ghPrView with no `number`.
#
# The failure is invisible from outside: `repo` still arrives, because
# openapi.json declares a default of "riftwing" for that parameter, so the
# request looks half-correct and Rewst reports the run "completed". Four runs
# were lost to that before anyone read a node trace.
#
# Refusing rather than deleting, so an old invocation says why instead of
# starting a run that cannot work.
set -euo pipefail
cat >&2 <<'MSG'
fire-pr-drive.sh does not work and has been superseded.

  A webhook run leaves CTX.INPUT empty, so pr-drive dies at ghPrView and
  Rewst still reports the run completed.

Use instead:

  python3 scripts/drive-pr.py <pr-number> --message "fix: ..."

It starts the run via POST /workflows/{slug}/run and prints the node trace.
MSG
exit 1
