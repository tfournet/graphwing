# Run-control activation: recovery and next milestone

## Issue #187 ownership and call-path baseline

This baseline is pinned to `origin/main` `631e10f9fa5ae4aa7a0ae511161e1a7faab99016` and the owner ruling in issue #184. `scripts/drive-pr.py` first calls `graphwing-run-control-initialize`, then starts `graphwing-pr-drive`. Publication-resolved `action.subworkflow` references mechanically reach `state`, `consume`, the `authorize` operation implemented by `consume-authorization`, `reconcile`, and `transition`. The older `graphwing-run-control-authorize` graph has no caller and is compatibility-only. `scripts/publish_graphs.py` holds the complete graph, pin-source, and daemon-symbol classification; `RunControlOwnershipBaselineTests` derives the graph references and server symbol set instead of trusting this prose.

The current daemon business-policy owners are `run_control_evaluate`, `_run_control_checkpoint`, `RUN_CONTROL_MAX_ATTEMPTS`, `RUN_CONTROL_CONSTRAINT_SIGNALS`, and `RUN_CONTROL_GAMING_SIGNALS`. Normalized-fact helpers, non-bypassable continuity checks, and v1 local settlement/ledger compatibility are classified separately. The challenge generator/read is active hard safety; the exact Rewst request HMAC, descriptor match, and process-local claim/consume enforcement primitives remain dormant until the dispatch call path invokes them. This inventory describes the migration debt; it does not legitimize daemon-owned policy or local durability. Rewst workflows own aggregate budgets, immutable attempt history, checkpoint interpretation, continuation, handoff, restructure, park, and terminal outcomes under [ARCHITECTURE.md](../ARCHITECTURE.md).

The checked-in v1 graph payloads still carry historical `description: "Dormant."` and `spec.meta.dormant` fields. Reachability and publisher pin resolution prove those labels false for the active chain, but this no-runtime-change slice does not rewrite graph JSON. The source classification above is canonical for migration planning until a behavior-bearing graph version is separately approved.

Migration is versioned and non-inferential: v1 in-flight records remain readable and finish under v1 or are explicitly parked, but are never inferred or guessed into v2. V2 uses new namespaces and cannot activate while an unresolved v1 reservation exists for the same run. No v1 graph, endpoint, ledger path, or historical record is removed in this baseline slice.

Source implementation and high-confidence Graphwing PR merges are authorized. Deployment, OpenAPI re-import, workflow publication, live canaries, and historical cleanup each remain separately approval-gated. This slice performs none of them and changes no runtime behavior.

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
12 s, USD 0.248882. Reconcile then charged the full envelope because `turns` was null:
`_normalize_native_usage` passes no turn count for any launcher by design and
the reconcile contract required all four aggregates.

Decision (Phase 1 of the remaining-work plan): keep the adapters honest and
charge only what a receipt lacks. `usage_check.complete` needs wall time and
tokens, the dimensions every adapter reports; a new `usage_fill` node takes
each dimension from the receipt when reported and from the reservation
envelope otherwise; `charged_usage` and the evaluator evidence use the filled
numbers, and `reconciliation.filled` records which dimensions were charged at
the envelope. `/v1/run/control/validate-receipt` accepts unreported turns and
cost and returns `filled`. The evaluator is unchanged and never sees nulls.

## Kicked runs read their inputs, 2026-09-02

The engine populates `INPUT` only for form triggers
(`rewst-go/services/worker/engine/trigger_context.go`); a webhook kick lands
in `CTX.TRIGGER.webhook.body`, which is why every kicked run died at `ghPrView`.
`graphs/pr-drive-run-control.json` now starts with a `run_input` builder right
after `join_start` that emits the thirteen pr-drive inputs plus `run_control`
from `CTX.INPUT` when `CTX.INPUT.repo` is set and from the webhook body
otherwise, and every other template reads `CTX.run_input.*` (58 references
rewritten). The kick body `pr_continue` posts already carries exactly those
fields. `kick_token` passes through untouched and is never hashed or logged.
The `hook` trigger stays disabled until the tenant proof enables it. Pinned by
`test_run_input_is_the_only_reader_of_ctx_input_and_falls_back_to_the_webhook_body`.

## Phase 1 proven live, 2026-09-02

Lifecycle p1 (run 71512ed5, reconcile child via `rc_reconcile`): a real
claude-opus-5 writer fixed PR #155 and the reconcile committed
`kind: terminal_receipt`, `terminal_status: succeeded`, `charged_usage`
`{turns: 50, wall_seconds: 13, tokens: 92280, provider_cost_usd: "0.250434"}`
with `reconciliation.filled.turns: true`, evaluator evidence `terminal`,
revision 3. Cosmetic follow-up: `reconcile_mode.authority_loss_reason` still
reads `receipt_evidence_unusable` on a terminal receipt and lands in the
transition delta; it should be null when the kind is `terminal_receipt`.

## Phase 2 proven live, 2026-09-02

Lifecycle p4 (runs 48cb8b73 and 1c84391d, PR #161): run 1's writer fixed one
of two bad files, checks stayed red, the retry leg reserved attempt 2 and
kicked the sibling's own webhook (`pr_drive_run_control_hook_url`, trigger
`0d9c348d…`); run 2 started from the webhook, `run_input` read every field from
the body including `work_kind`, the daemon launched claude under the carried
continuity (ledger: kick accepted, launch pinned, settlement terminal), and
reconcile committed `kind: terminal_receipt` with charged usage
`{turns: 50 (filled), wall_seconds: 9, tokens: 61423, provider_cost_usd:
"0.2318755"}` and a null loss reason.

Two fixes on the way: a writer's `succeeded` receipt no longer ends the
lifecycle (reconcile marks the state terminal only on `verified_outcome`, so
attempt 2 can reserve while the PR is still red; run d0aaae1c fenced on this),
and `reconcile_mode.authority_loss_reason` is null on a terminal receipt. One
procedure rule learned again: after any `openapi.json` change, run
`scripts/reimport_integration.py` before the tenant proof, or Rewst drops the
new request fields on the floor (run 6b9a77a3 lost the route inputs that way).

## Phase 4 proof and two platform findings, 2026-09-02

The #141 proof (implement-slice with every launcher stubbed to a missing
binary, one-ticket slice on PR branch `rc-validation/slice-phase4-20260902`)
never reached the fallback gate: run cf127daf died at `agent` with
"launcher is required". Cause: Riftwing's `transforms.lookupTable` returns a
matched entry `value` verbatim (`rewst-go/services/worker/nodes/
lookup_table.go`), evaluating only `input` and `defaultValue`. All nine lookup
nodes the Sep 1 routing refactor (543ef8e) added to implement-slice hold AST
values, so every implement-slice run since then has failed at `agent`. The
fixture interpreter in `test_server.py` evaluates the selected entry, which is
why the suite never saw it. Tracked as the "lookupTable entry value" issue;
the fix converts the nine nodes to objectBuilder conditional chains and adds a
lint. The #141 proof reruns after it lands.

Codex spike (#151): not feasible. Codex run from a sealed memfd fails every
`exec_command` and `apply_patch` verification with "No such file or
directory", with or without an external code-mode host; its internal
sandboxed re-exec resolves its own executable to the deleted memfd path. The
app-server JSON-RPC protocol itself works and exposes thread id, final
message, and a full token breakdown. Decision pending: same-uid private-copy
pinning for codex only, or an upstream codex issue. `go_coding` should route
to claude until then.

## Phase 4 proven live, 2026-09-02

After #165 (lookup entries evaluated) and #166 (picks report `value`; the
publish linter rejects `result` after a write unless something reads that
write), run 814e4f44 with every launcher stubbed to a missing binary went
primary writer → `missing_binary` receipt → `fallback_eligibility` →
`if_initial_fallback_eligible` pass → `fallback_route` (codex, availability
fallback) → `agent_fallback` launched → `record_fallback`. #141 is closed by
that run. Two more platform rules are pinned locally now:
`test_lookup_table_entries_are_never_ast` and
`test_mutation_outcome_reporters_read_the_write_they_report`.

## In-run retries: pr-drive-loop, 2026-09-02

Rewst allows no cycles outside `logic.loop` (E800), requires `maxIterations`
on a loop whose body waits (E812), and forbids body nodes routing back into
the loop (E105). Within that, `graphs/pr-drive-loop.json` replaces the kick
chain with one run: route once, then a three-slot loop whose each pass
re-reads the PR, skips when green, otherwise reserves an attempt through the
pinned state and consume children, launches the writer under it, settles from
the `wait` body, reconciles, tests, commits, and pushes; the shipped final
tail runs once after `done`. Nothing cross-run remains in it: no kick, no
webhook input fallback, no carried continuity.

Two engine rules learned on the way, both pinned by lints: a `logic.filter`
downstream of a loop judges only the loop item (`filter.go
buildFilterEffectiveItem` rebuilds its input from `ITEM` and `LOOP`), so
loop-adjacent branching uses `logic.switch`, which reads the raw payload; and
the loop's each and done handles join the handle vocabulary.

Proof (run 5fcecaed, PR #168, stub codex that edits one file and reports
ok): three slots, three distinct attempt ids, each consumed, launched,
settled `terminal_receipt`, reconciled, tested, committed, and pushed; the
final tail refused the draft PR as designed. The seat-only recipe
`rc-validation-compile` bound the final test to the validation repo.

## Activation, 2026-09-02

Decided: `pr-drive-loop`'s topology becomes `graphs/pr-drive.json` itself.
`graphwing-pr-drive` is now run-control reservation-backed end to end, up to
three in-run attempts, no cross-run kick. Landed in two PRs rather than one
because the combined diff (280KB) cleared `server.py`'s own
`REVIEW_MAX_DIFF_BYTES` (258048 bytes) — the same ceiling graphwing enforces
on diffs it reviews elsewhere applies to its own PRs, since
`test_landed_phase6_and_followup_candidates_fit_the_review_maximum` measures
the actual worktree diff, not a fixture.

1. Retire the two dormant siblings first (`graphs/pr-drive-run-control.json`,
   `graphs/pr-drive-loop.json`, `scripts/rc-drive-pr.py`): pure deletion,
   `pr-drive.json`'s shipped content untouched.
2. Swap `pr-drive.json`'s content to the loop shape, rewrite
   `scripts/drive-pr.py` as the single front door (initialize once, start
   `graphwing-pr-drive` once; no more outer Python attempt loop, no
   `--kick`/`--attempts`-as-kick-budget confusion), and update every test
   that pinned the old flag-off wiring.

Each PR's own diff came in under 145KB, following the precedent in
`test_server.py`'s `_DURABLE_FOUNDATION_BASE_COMMIT`/`_LANDED_COMMIT`: advance
both together (`BASE` becomes the previous `LANDED`) so each PR is measured
from a fresh baseline instead of accumulating the whole history.

Known gap, carried into `docs/USING.md`: nothing waits between one attempt
slot's push and the next slot's findings read, so a slot can act on a still-
stale blocking finding right after a push. The old cross-run driver had a
`wait_for_fresh_review` poll between separate Python-level attempts; an
in-run loop has no equivalent without its own bounded sub-loop, and building
one unproven wasn't in scope here. `scripts/drive-pr.py` still runs its own
pre-run audit-settle check before starting the graph at all, which covers
the common case (driving a PR whose audit just finished) but not a slot
racing a reviewer mid-run.

The form's `attempt`/`max_attempts`/`kick_url`/`kick_token` inputs, dead
leftovers from the old kick-chain shape that nothing in the loop graph reads,
are removed. Nine `herdr_*` diagnostic-noop node labels lost during the loop
graph's original construction are restored (only `herdr_final_fail` had kept
one); they are what make a failed run's stopping point legible in the trace.

## Live proof and two real defects, 2026-09-02 evening

Recreated the disposable validation harness (`/home/tim/work/gw-rc-validation-clone`,
a fresh clone of this repo; `graphwing-rc-validation` in `repos.json`;
`rc-validation-proof` in `tests.json`) that a prior session's cleanup had
removed, mirroring PR #168's pattern: a throwaway draft PR (#172, closed) with
one posted blocking finding whose remedy is a one-line, easily-checked fix.

First proof attempt (route resolved to codex by default for `go_coding`;
reran with `--work-kind typescript_coding` to get claude, since #151 already
found codex infeasible under the pinned launcher) surfaced a real defect:
`switch_needs_fix` read `TASKS.iter_findings.data.all_green`, which
`/v1/gh/pr/findings` folds in from GitHub check status alone, not the
findings-derived `blocking` verdict. A red PR with a passing CI check read as
green; every attempt slot skipped it. This predates activation (already in
`pr-drive-loop.json`) but the first loop proof (run `5fcecaed`, PR #168)
happened to fail its own CI check for an unrelated reason (invalid graph
JSON fixtures), so `all_green` was coincidentally false there and the bug
never showed. Fixed: `iter_snap` now maps `needs_fix` from `.data.blocking`.

Second attempt (after republishing) correctly identified the PR as needing a
fix and completed two full reserve→launch→settle→reconcile→test cycles, but
hit an HTTP 502 launching the writer on the third — a transient Rewst
gateway error. `agent` had no outgoing edges at all, so the loop iteration
died there uncaught instead of reaching `rc_failure` like every sibling
action node's failure handle already does. Fixed: `agent failure ->
rc_failure_join`.

Third attempt's own test recipe was wrong, not graphwing: `rc-validation-proof`
used `cwd: "."`, which runs against the daemon's own process directory, not
the target repo — `cwd` in a `tests.json` recipe is a repo *name*, resolved
through `repos.json` the same way `riftwing-local-gates` does it
(`cwd: "riftwing"`). Fixed the recipe, not the graph.

Fourth attempt, both fixes and the corrected recipe in place, run `4e9c5b39`:
slot 1 reserved an attempt, launched a real claude-opus-5 writer, which read
the finding, added the fix, passed the named test, committed (`b3cd7489`),
and pushed — the complete cycle, through the activated single front door.
Slots 2-3 correctly saw the PR as still "blocking" (the throwaway finding
comment never gets cleared without a real reviewer re-grading it) and
correctly no-op'd once there was nothing left to commit. The final tail
correctly refused merge evidence because the proof PR was a draft
(`"PR is not remotely ready for final evidence"`) — expected behavior for a
draft PR, not a bug. Closed #172 without merging.

Landed as their own PRs (#173, #174) rather than folded into the activation
PR, since each was found and fixed after #171 had already merged.

## Next bounded task

Codex writers now execute a job-private sibling copy of sealed launcher
bytes (`codex` + `codex-code-mode-host`) so `current_exe()` is a real path.
`go_coding` remains Codex. Live canary of a real file-editing `go_coding`
run is still required. The MCP build-and-export loop for the catalog remains
open.
