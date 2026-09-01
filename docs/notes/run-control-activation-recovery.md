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
  The settlement checkpoint below widens the closed set to four fields by adding
  `launch_descriptor_sha256`.
- `86cb6e0` resets the fixed review-size boundary to merged foundation `f242c90`.
  It does not raise the 258,048-byte limit; new changes are measured independently
  from the already-reviewed foundation.

Verified at `86cb6e0`: focused continuity and size-boundary tests 4/4, normal suite
589/589, missing-launcher suite 589/589, Python compilation, 13 JSON parses,
`actionlint`, `git diff --check`, and gitleaks. The worktree is clean and three
local commits ahead of `origin/main`; nothing is pushed, deployed, or published.
The rejected wiring remains archived only at
`archive/pr-drive-run-control-wiring-98c5d69-blocked`.

## Settlement ops (this checkpoint)

Two provider-free deterministic ops close the cross-run lifecycle on this host.
Neither touches a graph; `graphwing-run-control-reconcile` stays the durable
authority and verifies every field it is handed against Rewst datastore state.

- `POST /v1/run/control/settle` takes `{run_control, receipt}` where `receipt`
  is the exact body the `wait` webhook received, and
  returns the closed reconcile request (`kind`, `run_control_id`, `receipt`,
  `authority_loss_reason`, `replay`). The projected receipt uses the exact key
  order the reconcile graph's canonical hash expects. Only a receipt this daemon
  sealed at the job's terminal transition is usage (`authorized_receipt_usage`);
  a tampered body is refused, the `/v1/pr/continue` ACK is refused by shape, and
  a receipt whose authority a restart erased, or whose route is outside the
  run-control policy, closes as `authority_lost` rather than inventing usage.
  Settlement is one-time per reservation and per job in a durable ledger under
  `$GRAPHWING_HOME/run-control/`: an identical repeat replays, anything else is
  `409 settlement_conflict`.
- `POST /v1/run/control/authority-loss` takes `{run_control}`. `pr_continue`
  claims the reservation's single kick in the same ledger before posting
  (`409 continuity_already_used` on a second kick) and records the kick result
  after. A definite kick failure is lost at once
  (`continuation_kick_failed`); an accepted or unknown-result kick waits out
  `RUN_CONTROL_KICK_LOSS_GRACE_SECONDS` (7,200 s, twice a pr-drive run's bound)
  before `kicked_run_never_settled`, so a slow live run is never charged twice.
  Kicks this daemon never recorded cannot be declared lost by it.

Descriptor-hash source, resolved: the reconcile graph requires
`receipt.launch_descriptor_sha256` to equal the reservation's persisted hash. A
value run N+1 read back from that same durable state would pass trivially and
bind nothing, so the hash travels from the reserving run. The closed continuity
is now four fields, `run_control_id`, `attempt_id`, `authorization_id`,
`launch_descriptor_sha256`, validated identically by `/v1/pr/continue`, settle,
and authority-loss, and forwarded verbatim as `CTX.INPUT.run_control`.

Validation step 2 is a standing test, `PrDriveFlagOffTraversalTests`: every
node in `graphs/pr-drive.json` executes when `run_control_enabled` is unset or
false, so the whole graph is the flag-off traversal and it must equal merged
`main`'s (`f242c90`) node-by-node and edge-by-edge. The test also rebuilds the
blocked inline mode/switch/join shape on `main`'s graph and proves the check
rejects it. Consequence: run-control gating cannot live inside the shipped
pr-drive path. It goes in a separate slug or a publish-time render, so the
shipped `graphwing-pr-drive` stays byte-identical and step 3's zero-write
assertion means something.

## Next bounded task

Author the enabled pr-drive shape as a separate dormant catalog graph
(`graphs/pr-drive-run-control.json`, slug `graphwing-pr-drive-run-control`) that
`publish_graphs.py` publishes only when asked by name, never with `--only all`
or `--only pr-drive`. Under a carried `CTX.INPUT.run_control` the agent leg must
not reserve again (the state graph refuses a second outstanding reservation); it
launches the writer, settles through `/v1/run/control/settle` from
`TASKS.wait.request.body`, and feeds the result to the pinned reconcile child.
The continue leg reserves, consumes, kicks with the four-field continuity built
from the state subworkflow's outputs, and on kick failure closes through
`/v1/run/control/authority-loss`. Prove the shape with provider-free structural
tests; `graphs/pr-drive.json` stays untouched. Do not publish until the flag-off
traversal test and both suites are green and an isolated tenant is available.
