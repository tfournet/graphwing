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

## Live tenant findings, 2026-09-01

Publishing to org `485bb4f1…` (tim-graphwing, not Rewst Internal) after
deploying this branch's daemon and re-importing the integration (version 22).
Step 3 held: the published `graphwing-pr-drive` equals the local file node for
node and edge for edge, and its missing-PR smoke executed five nodes, none a
datastore operation. There is no datastore read API, so that trace is the
zero-write evidence.

The first live runs of the dormant chain found three defects in the merged
foundation, none visible to fixture tests:

1. **Handle names.** `run-control-initialize` and `run-control-reconcile`
   wired their validate action's success as `out`; the platform emits
   `success`/`failure` for actions, so the run ended "completed" after the
   validate call with nothing done. Fixed; `GraphEdgeHandleVocabularyTests`
   pins every edge to its node type's handle vocabulary.
2. **Child budget.** Every `action.subworkflow` gave its child 120 s. The
   engine refuses to start a child whose first node cannot fit in the parent's
   remaining Temporal budget ("required=7m12s ... remaining=1m59s"). All
   subworkflow timeouts are now 900 s; the lint floor is 480 s.
3. **Child inputs.** `inputMapping.values` were AST wrappers
   (`{"kind": "literal", "value": "{{ ... }}"}`). The engine renders each value
   as a template and passes the result as `INPUT.<key>`, so the child received
   objects and every comparison against `INPUT` failed silently; the transition
   child fell through all its gates and wrote nothing. Values are now plain
   templates, as the engine's own node docs show (`rewst-go/services/worker/
   nodes/subworkflow.go`); the lint forbids wrapped values.

After those fixes initialize ran end to end, and the later runs found six more,
each fixed with a pinning test in `GraphEdgeHandleVocabularyTests`:

4. **Handoff gate.** The state graph forced every reservation through the
   cross-model handoff gate, so an evaluator-approved same-model continuation
   could never reserve. The gate now passes when the route is unchanged or the
   handoff is closed with an allowed reason (run a777cbbc).
5. **Gate feeders.** Four commit gates (state `post_*`, reconcile `post_*`)
   and the consume graph's `precheck_gate` read a payload that never carried
   the fields their rules test (an identity builder or another filter's
   verdict). Each now reads a builder that re-emits the decision fields. The
   shipped `implement-slice` has the same defect on its provider-availability
   fallback gate; that is #141, not edited here (runs c1697499, 488f13fa).
6. **Object equality.** `receipt.route == reservation.route` crashed the
   engine ("comparing uncomparable type map"); routes compare field by field
   (run 12386f9a).
7. **Authorization id.** The state graph names authorizations
   `rca1-<sha256 of the JSON-quoted run id>-<attempt hex>`; the daemon's
   continuity check now derives the same value (`run_control_authorization_id`).
8. **Consumed-record TTL.** The consumed authorization expired after 300 s and
   reconcile ran 306 s after consume. Consumed records no longer expire; only
   the issued record does (run 845ed190).
9. **Nested budgets.** A child that itself calls a 900 s subworkflow needs
   about 27 minutes of remaining budget; the sibling gives its children 2400 s
   (run c6e82989).
10. **Reconcile routing.** An authority-loss input committed and then fell
    into the receipt-replay branch and fenced; a replay against a closed
    reservation rendered an empty datastore key; a receipt downgraded to
    authority loss recorded no receipt identity, so its replay read as a
    conflict. All three are routed or recorded correctly now (runs e4613652,
    e6de7fbc).

## Validation results, 2026-09-01

Steps 3 and 4 are done in the isolated tenant with the codex binary stubbed to
a missing path (a systemd drop-in, since removed), so no provider ran:

- **Agent leg, twice** (lifecycles a6 and a9): initialize → reserve → consume →
  `graphwing-pr-drive-run-control` with the four-field continuity. The daemon
  pinned the launch to the reservation, the stub failed with a sealed
  `missing_binary` receipt, the wait node received it, `rc_settle` projected
  the terminal receipt, the in-run reconcile child passed every identity and
  consumed-authorization check, charged the full envelope as
  `receipt_evidence_unusable`, and the run ended at `herdr_receipt`.
- **Replay and conflict** at both layers: an identical settle replays, a
  different descriptor or a second job conflicts, authority loss on a settled
  reservation is refused; a replayed reconcile of a closed reservation is
  ignored rather than fenced.
- **Lost kick** (lifecycles a7 and a8): reserve → consume → `/v1/pr/continue`
  to a kick URL that fails → daemon `continuation_kick_failed` → reconcile
  committed the authority loss; a second kick under the same continuity is
  refused; the kick fingerprint the daemon recorded equals the one the
  reservation was made with.

Policy decision, 2026-09-01: same-model retries are bounded by the attempts
budget, not by a forced model switch. The evaluator no longer emits
`same_model_continuation_exhausted` (the code stays in the vocabulary) and the
state graph's reservation gate no longer requires
`same_model_continuation_used == false`; the flag is still tracked.
`GRAPHWING_RUN_CONTROL_GRACE_SECONDS` overrides the two-hour loss grace for
validation runs only; the default is pinned by a test.

## Retry leg and timer path, simulated 2026-09-01

With the same-model limit relaxed, a stub codex (edits one tracked file,
prints `thread.started` and an `agent_message` with `status: ok`, no model)
drove `graphwing-pr-drive-run-control` through its retry leg in one run
(lifecycle b4, run 75c89e0f): writer receipt ok → settle → in-run reconcile
(revision 3) → named test → commit → push → checks still red → kick
fingerprint → `would_kick` gate → reserve attempt 2 on the same route →
consume → kick to a failing URL → daemon `continuation_kick_failed` →
reconcile (revision 6) → budget stop. The daemon's recorded kick fingerprint
equals the one the reservation was made with.

The timer path ran against the real pr-drive webhook with
`GRAPHWING_RUN_CONTROL_GRACE_SECONDS=120` (lifecycle b7, run 2bb816c3): the
kick was accepted, the kicked webhook run started and died at `ghPrView`
(webhook runs still create no `CTX.INPUT`), authority loss was refused inside
the window (`kick_within_grace`), and after it the daemon returned
`kicked_run_never_settled` and reconcile committed revision 6.

Two more findings on the way:

11. **Kick header.** `pr_continue` and `slice_continue` posted kicks with only
    the callback token header; a Rewst webhook trigger checks
    `x-rewst-secret`, so every real kick was refused. Kicks now send the kick
    token under both headers (run ccf0dd9e).
12. **Seat named tests.** Named tests run in the daemon's home, where only
    `server.py`, `test_server.py`, and `setup_tunnel.py` exist;
    `graphwing-compile` fails there on `install.py`. `graphwing-seat-compile`
    compiles what the seat actually has. Writer commits also need a git
    identity in the allowlisted checkout, because the daemon's `HOME` carries
    none.

## Step 5, real provider, 2026-09-02

Codex first. Under the pinned launcher codex 0.151.0 cannot edit files: it
resolves its code-mode host as a sibling of the sealed memfd, and on this
build every tool call routes through that host. Probed outside the daemon:
no env override, no config key (`--strict-config` rejects every
`code_mode_host.*`), only `codex app-server --code-mode-host <URL>` accepts an
external host. Tracked as #151; codex writers on this seat are text-only until
then. Its run (d7a75bec) still accounted correctly: 49,394 tokens settled,
full envelope charged, revision 3.

Claude then. Two more seat defects and one daemon defect:

13. **Credentials.** The unit's `HOME` is the clean profile, which holds no
    codex or claude credentials. Drop-ins `codex.conf`
    (`CODEX_HOME=/home/tim/.codex`) and `claude.conf`
    (`CLAUDE_CONFIG_DIR=/home/tim/.claude`) now mirror the existing grok one.
14. **`--allowedTools` is variadic** on Claude Code 2.1.258. The writer argv
    put the prompt right after `--allowedTools Bash`, with nothing between
    when the routed effort is "default", so the prompt was swallowed and
    claude died with "Input must be provided". It is one token now,
    `--allowedTools=Bash` (seat job 2d8999db).
15. **Usage shape.** 2.1.258 adds `usage.output_tokens_details` and
    `modelUsage.thinkingTokens`; the exact-set validators reported a real
    opus fix's usage as `usage_malformed`. Both fields are accepted and
    validated, with the real result as a fixture (seat job 85890fb8).

Results (lifecycles c4 and c5, runs aedd2ddd and 6ea9ae53): a real
claude-opus-5 writer under a reservation read the blocking finding, deleted the
bad file, passed the named test, and the daemon committed and pushed; PR #150's
CI went green on that commit. On c5 settle carried real usage: 92,137 tokens,
12 s, USD 0.248882. Reconcile still charged the full envelope because `turns`
is null: `_normalize_native_usage` passes no turn count for any launcher by
design ("no adapter trusts provider-authored turn counts"), while the reconcile
contract requires all four aggregates. That is a policy contradiction, not a
bug in either half. Recommended: charge the envelope only for the dimensions a
receipt lacks (turns here), keep the real tokens, wall time, and cost, or have
adapters count turns from their own event streams. Decide before real runs are
billed.

## Next bounded task

Resolve the turns policy above (reconcile graph `usage_check` and
`attempt_entry.charged_usage`, plus `run_control_validate_receipt`), then run
one more real claude lifecycle and confirm `kind: terminal_receipt` with
partial-envelope charging. Then #151 for codex, #141 for implement-slice, and
the webhook `CTX.INPUT` gap so a kicked run can actually continue.
