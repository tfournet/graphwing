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

## Cross-run binding, decided 2026-09-01

Defect 2 (caller-precomputed evidence) had no honest answer for the continue
leg: run N cannot fingerprint the writer request run N+1 will render. The
reservation now binds what run N does know, and the daemon binds the rest:

- **Kick fingerprint.** `/v1/pr/continue` with continuity returns
  `kick_request_sha256`, the SHA-256 of the canonical kick request minus
  `run_control` (its descriptor hash covers this value) and `kick_token` (a
  secret never belongs in a durable descriptor). For the continue leg the
  reservation descriptor's `exact_request_body_sha256` is this value; the
  reserving run renders the kick request, hashes it the same way, reserves,
  then sends exactly that request. The ledger records the fingerprint with
  the kick for isolated-tenant comparison against durable state.
- **Launch pinning.** `/v1/agent/run` accepts the four-field `run_control`
  forwarded verbatim from `CTX.INPUT.run_control`. The daemon refuses a launch
  whose continuity differs from what it kicked (`run_control_mismatch`), a
  second launch (`reservation_already_launched`), a launch after settlement
  (`reservation_already_settled`), or one under a definitively failed kick
  (`continuation_kick_failed`), and otherwise pins the job to the reservation
  before the job record exists. A reservation this daemon never kicked (the
  same-run agent leg) may launch once; the unguessable trio binds it there.
- **Settlement names the job.** Once a reservation is launched, only that
  job's sealed receipt can settle it.
- **Run-started signal.** Authority loss on a launched reservation is refused
  while the job is queued or running (`run_started`), waits out the grace
  window after the job ends (`launch_within_grace`), then charges the full
  envelope as `launched_run_never_settled`. The kick-only grace path still
  covers a kick whose run never launched.

## Enabled shape, authored 2026-09-01

`graphs/pr-drive-run-control.json` (slug `graphwing-pr-drive-run-control`) is
pr-drive's topology plus the run-control legs, kept as a separate dormant slug
so `graphs/pr-drive.json` stays byte-identical to `main`.
`PrDriveRunControlGraphTests` pins the exact node and edge deltas, the child
pins, the fenced failures, and that the fingerprinted kick and the sent kick
are the same request.

- **Start.** API only, `{ "input": { ...pr-drive inputs..., "run_control": {four
  fields} } }`. Form and webhook triggers are disabled. Run 0 is an operator
  initialize + state + consume; every later run is kicked by its predecessor.
- **Agent leg.** `agent` carries `run_control` verbatim; the daemon pins the job
  to the reservation. `wait out` now goes `rc_settle` (`/v1/run/control/settle`
  with `TASKS.wait.request.body`) → `rc_reconcile` (pinned reconcile child fed
  the settle result) → `receipt`. Wait timeout and failure keep their original
  edges; a launched reservation the run cannot settle is closed later by the
  timer through `/v1/run/control/authority-loss`.
- **Continue leg.** `if_green2 fail` → `rc_kick_fingerprint`
  (`/v1/pr/continue/fingerprint`, the same fields as the kick minus
  `kick_token`) → `rc_kick_gate` on `would_kick` (no reservation for a kick
  `pr_continue` would refuse) → task and callback hashes → `rc_continue_state`
  (reserve with `exact_request_body_sha256` = the fingerprint, endpoint
  `/v1/pr/continue`, candidate route from `TASKS.route.data`, a literal
  per-attempt envelope of 50 turns, 600 s, 2,000,000 tokens, USD 25) →
  `rc_continue_consume` → `rc_continuity` (four fields from the state child's
  outputs) → `continue` with `run_control`. Kick failure → `rc_continue_loss`
  (daemon authority-loss; `continuation_kick_failed` for a recorded failure)
  → reconcile; if the daemon never saw the kick → reconcile
  `continuation_action_failed` directly, which run N does know first-hand.
- **Fence.** Every run-control action and child routes failure to
  `rc_failure_join` → `rc_failure`, the same hard fence the durable graphs use.
- **Publish.** `publish_graphs.py --only pr-drive-run-control` republishes the
  durable chain first (transition, consume-authorization, state, consume,
  reconcile), pins the child IDs, then publishes the slug. `--only all`
  includes it last, like every dormant graph (the catalog contract publishes
  every graph file once); `--only pr-drive` never reaches it.

Runtime assumptions this checkout cannot verify: `logic.filter` after an action
reads `data.*`; subworkflow outputs read as `TASKS.<alias>.result.<child alias>`;
an action config value that renders an object (`{{ CTX.INPUT.run_control }}`,
`{{ TASKS.wait.request.body }}`) is sent as that object. All three follow the
shipped graphs' existing usage; import in the isolated tenant is the proof.

## Next bounded task

Validation step 3 in the isolated tenant: publish with
`--only pr-drive-run-control`, run the existing pr-drive smoke against the
shipped `graphwing-pr-drive`, and assert zero writes to any
`graphwing_run_control_*` namespace. Then step 4 with a stub launcher:
initialize + state + consume for run 0, start `graphwing-pr-drive-run-control`
with that continuity, and verify reserve → launch pin → settle → reconcile
across two chained runs, then authority loss on a lost kick and on a launched
run that never settled. Nothing here is pushed, published, or deployed; the
shipped pr-drive graph is unchanged.
