# Run-control activation: recovery and next milestone

Status, 2026-09-01. The dormant durable run-control foundation (PR #139) is merged
at `f242c90`. The first pr-drive wiring attempt (`98c5d69`) is blocked and archived
on local branch `archive/pr-drive-run-control-wiring-98c5d69-blocked`; do not push
or revive it. Review found three defects that are design gaps, not patches:

1. **Flag-off behavior changed.** It rerouted the `wait` pending/timeout/failure
   edges and added an `agent` failure edge through mode/switch/join chains, so the
   disabled path no longer matched the shipped pr-drive graph.
2. **Caller-precomputed evidence.** `exact_request_body_sha256`,
   `launcher_fingerprint`, `task_sha256`, `callback_binding_sha256` were all read
   from `CTX.INPUT.run_control.*`. The workflow must compute them from the request
   it actually renders and sends, or the reservation binds nothing.
3. **Kick ack treated as a terminal receipt.** It built a
   `terminal_status: "succeeded"` receipt from `TASKS.continue.data`. That is the
   synchronous acknowledgment of `/v1/pr/continue`, which only kicks a fresh
   pr-drive run at `kick_url` (`server.py` `pr_continue`). The attempt outcome
   does not exist yet when that response arrives.

## Missing piece: cross-workflow terminal-receipt lifecycle

A pr-drive attempt spans two runs: run N reserves and kicks, run N+1 executes and
observes the outcome. The foundation has no way to close run N's reservation:

- The kick payload (`pr_continue` → `kick_url`) carries no run-control identity.
  Run N+1 cannot name the reservation it should settle.
- No path reconciles a reservation when the kicked run never starts (kick
  accepted, run lost). Needs an authority-loss settlement driven by the next
  initialize or a durable timer, not by the run that died.
- The agent leg's terminal receipt must be built from the live `wait` webhook
  body inside the run that made the reservation; only the continue leg crosses
  runs.

## Validation order after the dormant foundation

Provider-free first, isolated tenant second, real provider last:

1. `GRAPHWING_HOME=. python3 test_server.py` — HTTP contract, provider-free.
   (`native_run_control_contract_test.go` runs against `asteval` in the Rewst
   Graph runtime, not here.)
2. Graph-structural test, provider-free: with `run_control_enabled` unset or
   false, the traversed pr-drive node/edge sequence is identical to `main`'s
   graph. This is the regression the blocked commit failed.
3. Isolated tenant, dormant: publish the durable chain plus pr-drive via
   `publish_graphs.py`, run the existing smoke, assert zero writes to any
   `graphwing_run_control_*` datastore namespace.
4. Isolated tenant, enabled, stub launcher: verify reserve → consume → terminal
   receipt across two chained runs, then authority-loss on a lost kick.
5. Real provider in the isolated tenant only after 1–4 are green.

## Current checkpoint

The first bounded task is complete on `feature/run-control-activation`:

- `7d725e5` records this recovery plan.
- `7ed66b7` adds optional closed `/v1/pr/continue` continuity:
  `run_control_id`, `attempt_id`, and the matching `authorization_id`. It validates
  the trio before `kick_url`, forwards it as `CTX.INPUT.run_control`, preserves
  the legacy payload when omitted, and never treats the kick ACK as a receipt.
- `86cb6e0` resets the fixed review-size boundary to merged foundation `f242c90`.
  It does not raise the 258,048-byte limit; new changes are measured independently
  from the already-reviewed foundation.

Verified at `86cb6e0`: focused continuity and size-boundary tests 4/4, normal suite
589/589, missing-launcher suite 589/589, Python compilation, 13 JSON parses,
`actionlint`, `git diff --check`, and gitleaks. The worktree is clean and three
local commits ahead of `origin/main`; nothing is pushed, deployed, or published.
The rejected wiring remains archived only at
`archive/pr-drive-run-control-wiring-98c5d69-blocked`.

## Next bounded task

Implement provider-free run N+1 settlement of the named reservation from the live
terminal `wait` webhook receipt. Prove exact `run_control_id`/`attempt_id`/
`authorization_id` binding, one-time reconciliation, malformed receipt rejection,
and that the synchronous `/v1/pr/continue` ACK is never accepted as terminal
usage. Then implement conservative authority-loss reconciliation for an accepted
kick whose new run never starts. Do not activate or rewire production graphs until
both lifecycle paths are proven and the flag-off graph traversal remains identical
to merged `main`.
