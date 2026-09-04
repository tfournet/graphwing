# Using graphwing

Runs on the machine that has the git checkouts. Rewst org is yours (not Rewst Internal). Catalog git is [`tfournet/graphwing`](https://github.com/tfournet/graphwing).

Operator lock: [HUMAN-LOOP.md](HUMAN-LOOP.md). Do not implement in the grill pane.

## Sit down

Use your normal editor or terminal; optional Herdr session `graphwing`, tab `graph`, is a read-only job dashboard.

Allowlisted short names are `$GRAPHWING_HOME/repos.json`. Graph `cwd` / `repo` fields use those names, not paths.

## Structure a slice map

After Shortcut Ready, same tab, `/to-tickets` (from the `graphwing-loop` plugin; see [HUMAN-LOOP.md](HUMAN-LOOP.md) step 1 to install it). Graph does not Structure.

Index JSON in the app worktree (example: [`examples/slices.example.json`](../examples/slices.example.json)):

```json
{
  "tickets": [
    {
      "id": "01-login",
      "path": "slices/demo/01-login.md",
      "blocked_by": [],
      "kind": "build",
      "status": "open"
    }
  ]
}
```

Each ticket file is what the writer sees. `kind` is `build` or `decision`. `status` is `open` or `done`. Frontier is unblocked open `build` tickets. Serial: one frontier ticket at a time on the story branch.

Name a `tests.json` recipe for the story suite. Visual without a recipe has no per-slice gate, but a recipe is not browser/screenshot evidence; this catalog has no visual-proof workflow.

For riftwing, use `riftwing-local-gates`. That runs `.githooks/pre-commit` (gitleaks, lockfile) with `RIFTWING_HOOK_NONINTERACTIVE=1`. It does not run pre-push fetch or Shortcut.

## Fire implement-slice

Published slug: `graphwing-implement-slice`. Use the manual/form trigger or an authorized `/workflows/graphwing-implement-slice/run` caller with an `input` wrapper.

```json
{
  "input": {
    "repo": "riftwing",
    "branch": "story/your-branch",
    "index": "slices/demo/index.json",
    "ticket": "slices/demo/01-login.md",
    "commit_message": "feat: login tracer",
    "test": "your-named-recipe",
    "class": "mechanical",
    "size": "S",
    "kick_url": "https://engine.rewst.io/webhooks/custom/run?...",
    "e2e": "optional-named-e2e"
  }
}
```

`ticket` must already be on the frontier, or omit it and Graph takes the next build.

`ac_count` and `seams` feed `graphwing-routing-policy`, which may bump the size one step from countable features. Omitting them leaves the workflow policy blind and the floor unchanged, so send them honestly rather than to dodge a gate.

The legacy seat helper still exists:

```bash
scripts/fire-slice.sh payload.json
```

It POSTs to this host's `/v1/rewst/fire`, which proxies to the Rewst webhook. Do not use it as the current operator start or as rollout proof: observed webhook runs do not materialize `CTX.INPUT`, so graph inputs resolve empty. The URL and `hook_secret` stay in `$GRAPHWING_HOME/rewst-install.json`.

`kick_url` remains generic catalog wiring, but its next-run webhook uses the known-broken input mapping. It cannot continue the slice walk today; treat one manual/API run as one slice.

On red, files stay. No `gitRestore`. Three suite-reds or a second spec-review nack parks. You continue, discard (the only wipe), split, restamp size, or tag `decision`.

### Canonical native routing policy (issue #186, slice 3)

`graphwing-routing-policy` accepts only `class`, `work_kind`, `size`, `ac_count`, and `seams`. Native AST/object nodes validate and normalize those values, apply at most one size bump, select the current normal-v1 writer and class/effective-size budget, and choose zero, one, or two distinct opposing-provider reviewers. Writer effort remains work-kind-specific; reviewer effort is independently `medium` or `high`. Every present role carries one complete `route-execution-profile-v2` bound to the deterministic decision hash.

The graph's policy version is `workflow-normal-v1`, with `normal-v1` retained as its compatibility behavior label. `implement-slice` and `pr-drive` invoke the exact published workflow/version and feed their initial writers and routed reviewers from its v2 profiles. Same-session corrections reuse the successful receipt identity and never invoke policy again. `scripts/drive-pr.py` does not select a route or initialize aggregate policy; it submits intent for an existing Rewst-owned v2 run. The v1 endpoint/profile and the fallback/recovery branches remain for rollback and later migration. This source change performs no import, publication, provider call, or live canary.

### Native effort contract (Phases 2–3)

Direct `POST /v1/agent/run` and `POST /v1/review/run` accept optional `effort` with the closed values `default`, `low`, `medium`, `high`, and `max`. A supplied direct-call value records `effort_source: explicit`; omission records `requested_effort: default` and `effort_source: launcher_default`. Source `route` is available only when the request also supplies the exact closed `route_execution_profile` emitted by the selected route. That `route-execution-profile-v1` object binds route version, role, work kind, class/effective size, launcher/provider/model, and requested effort; runtime reconstructs it from the versioned route catalog and rejects unknown, missing, extra, or mismatched evidence before launch. Unknown effort text returns `400 bad_effort`; a known value outside the selected native profile returns `400 unsupported_effort` before process launch.

| launcher/model | accepted request values | effective effort |
| --- | --- | --- |
| Codex `gpt-5.6-sol` (and code-off-only `gpt-5.6-terra`) | `default`, `low`, `medium`, `high`, `max` | `default` → `high`; explicit values unchanged |
| Claude `claude-opus-5`, `claude-sonnet-5` (and code-off-only `claude-fable-5`) | `default`, `low`, `medium`, `high`, `max` | unchanged; `default` emits no flag |
| Grok `grok-4.6` | `default`, `low`, `medium`, `high` | `default` → `high`; explicit values unchanged |

Installed non-inference probes established Codex CLI 0.150.1 config syntax `-c model_reasoning_effort=<value>`, Claude Code 2.1.250 session syntax `--effort <low|medium|high|xhigh|max>`, and Grok 1.0.5 ACP placement `grok agent --reasoning-effort <none|minimal|low|medium|high|xhigh|max> ... stdio`. The embedded `grok-4.6` catalog declares default `high` and advertises `low`, `medium`, `high`, and `xhigh`. Graphwing therefore maps requested Grok `default` to native `high`, exposes `low | medium | high`, omits vocabulary-external `xhigh`, and rejects parser-only/model-unproven `max`. Writer and reviewer commands share the same profile validation while preserving `workspace-write`/`read-only`, `acceptEdits`/`plan`, and ACP always-approve/read-only differences. Phase 3 maps selected route effort and matching closed evidence into implement-slice and PR-drive initial/recovered writers, fallback writers, and routed reviews. Same-session corrections copy requested effort and `route_execution_profile` from their pinned receipt identity; they cannot redraw a route or change requested/effective/source/version. These local graph fixtures do not prove tenant import, publication, or live execution.

### Closed async review contract

`POST /v1/review/run` is a closed object: `repo`, `launcher`, `provider`, `model`, and `prompt` are required; only the documented optional effort, route evidence, async, budget/turn, and webhook fields are accepted. Unknown fields return `400 unexpected_fields` before launcher lookup or spawn. A synchronous `200` receipt and asynchronous `202` job carry the same closed `review-execution-v1` identity: canonical review kind, launcher/provider/model, requested/effective/source effort, optional exact route evidence, path-free wrapper fingerprint, repo/branch/head, prompt and diff hashes, budget/turn limits, and read-only permission profile.

`diff_sha256` covers the complete `git diff HEAD` bytes, never a prefix. Acceptance rejects over 258,048 bytes before launcher lookup. Codex/Claude use stdin and Grok uses ACP, so prompts stay off argv; runtime rechecks accepted bytes and identity before launch.

For a `202`, use the returned `poll` value (`GET /v1/review/jobs/{id}`) or `POST /v1/review/jobs/wait` with `{"job_id":"…","timeout_seconds":30}`. These surfaces positively accept only canonical review records; `/v1/agent/jobs/{id}` remains agent-only. Review effort provenance is separately closed: direct requests use `explicit | launcher_default`; `route` is canonical only with an exact `reviewer1 | reviewer2` route profile embedded in the sealed execution identity. Missing, writer-role, wrong-version, or mismatched profile evidence makes the request or later identity/job/receipt/runtime profile noncanonical. Acceptance serializes the canonical execution snapshot into a sealed process-local descriptor and retains the exact already-pinned wrapper descriptor in a private registry keyed by the unguessable server job ID. The worker loads that authority rather than trusting the mutable job, snapshot file, or their mutually consistent hash. Poll and wait expose only exact server-authored second-resolution UTC lifecycle timestamps; a malformed or hostile timestamp rejects the whole record and leaks no stored value. After acceptance, every ordinary `write_job` call against the existing review returns `False` under the per-job lock, including a structurally equal object or one whose keys were reordered; it does not invoke the atomic writer, replace the file, or touch persisted review bytes. The guarded queued-to-running transition authors one exact closed running record, including the server `started_at`, null `finished_at`/receipt/error/webhook values, and no unknown fields, and seals its canonical bytes and digest in private process authority. The final transition requires exact key/type/timestamp validation and canonical-byte equality with that full sealed running value. Any drift is preserved byte-for-byte, suppresses terminal success and callback, and is never repaired from stale state. Callback bookkeeping is a guarded webhook-only update against the exact committed terminal revision. Polling validates an exact closed receipt and projects allowlisted fields instead of returning stored receipt JSON. Invalid or injected records return sanitized `409` errors and leak no stored prompt, path, token, URL, provider output, or hostile timestamp.

That authority is deliberately ephemeral. If the daemon is replaced before completion, the private registry and sealed descriptors are gone and Graphwing does not reconstruct them from disk. Startup closes the review without a receipt or callback derived from stale evidence; poll/wait returns `409 review_authority_unavailable`. A fresh Rewst-owned continuation is required when that architecture exists. Completed canonical reviews remain pollable, while ordinary trusted writes cannot replace their terminal receipt. Review errors use the closed `ReviewError` shape with exactly bounded `error` and `code` strings. `POST /v1/review/run` documents a disjoint `409 oneOf`: `ReviewError` for busy/acceptance errors or `ReviewReceipt` for a synchronous execution-identity mismatch, so each real body matches exactly one branch. Launcher-fingerprint and missing-launcher `501` responses are closed `ReviewError` bodies, not receipts.

### Closed normalized usage receipt

Terminal agent and review receipts expose exactly one of two alternatives: a complete `usage` with `usage_diagnostic: null`, or `usage: null` with one non-null closed diagnostic. The Draft 2020-12 schemas encode that pair as an exclusive `oneOf`, so null/null and usage-plus-error are invalid. A complete `normalized-usage-v1` object has exactly `usage_version`, `fresh_input_tokens`, `cached_input_tokens`, `cache_write_tokens`, `output_tokens`, `reasoning_tokens`, nullable `provider_cost_usd`, `wall_seconds`, and nullable `turns_observed`. Counts are bounded from 0 through 1,000,000,000; cost from USD 0 through 100,000; wall time from 0 through 1,800 seconds; and observed turns from 0 through 80. These closed ceilings exceed the supported 80-turn/1,200-second job budget while rejecting absurd provider numbers before conversion. Unknown provider cost is `null`, not estimated list-price dollars and not zero. Claude's reported API-dollar total is preserved when present; Codex and Grok native/subscription usage remains token-only unless their supported event contract exposes an API-dollar total.

Normalization is all-or-nothing and uses duplicate-rejecting JSON parsing at every object depth. Fractional and exponent literals are parsed exactly before bounds checks, then converted to the public JSON number only when conversion stays finite and cannot turn a nonzero cost into zero; explicit `0` and `0.0` remain valid. `NaN`, `Infinity`, and `-Infinity` are rejected by the JSON parser even in otherwise ignored fields. Missing, partial, negative, boolean, string, non-JSON numeric object, non-finite, oversized, duplicate-key, contradictory, or unknown-field native values return no aggregate and a stable diagnostic without raising. Claude Code 2.1.250 usage is accepted only from its closed, typed JSON terminal-success result: one `type: result` / `subtype: success` object with `is_error: false`. Required fields are `type`, `subtype`, `duration_ms`, `duration_api_ms`, `is_error`, `num_turns`, `result`, `stop_reason`, `total_cost_usd`, `usage`, `modelUsage`, `permission_denials`, `uuid`, and `session_id`. The complete optional set is `ttft_ms`, `ttft_stream_ms`, `time_to_request_ms`, `user_message_uuid`, `request_sent_wall_ms`, `time_to_request_from_spawn_ms`, `warm_spare_claimed`, `time_origin_ms`, `api_error_status`, `subagent_stats`, `queued_turn_count`, `structured_output`, `deferred_tool_use`, `terminal_reason`, `fast_mode_state`, `fast_mode_disabled_reason`, and `origin`. Their integer/number, nullability, boolean, closed nested-object, discriminated-union, and enum types match the installed 2.1.250 contract. Timing/count telemetry is finite, nonnegative, non-boolean, and capped at the shared 10,000,000,000,000 safety ceiling; bounded string metadata is control-free. Provider JSON is iteratively limited to 288 KiB, depth 32, 8,192 nodes, 2,048 containers, 1,024 entries per array/object, aggregate entry/string budgets, 1,024-character keys, and 256-KiB strings; excess or decoder recursion returns `usage_malformed`. All optional telemetry is validation-only. `stop_reason` is string or null. The required `modelUsage` object may be empty; normalized totals still come only from the authoritative `usage` object. A nonempty `modelUsage` entry requires `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`, `webSearchRequests`, `costUSD`, `contextWindow`, and `maxOutputTokens`; it may add only string `canonicalModel`, string `provider`, and `costBasis: list | managed | unknown`. These metadata fields are validation-only and never affect normalized token or cost totals. Missing required fields, wrong scalar types or values, error results, and extras fail closed. Codex CLI 0.150.1 requires its supported `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, and `reasoning_output_tokens` fields and maps all five; a missing counter is not fabricated as zero. Grok 1.0.5 ACP evidence establishes `totalTokens == inputTokens + outputTokens`; `thoughtTokens` is preserved separately and is not added to that total. ACP names cache counters but does not establish their inclusion in input, so nonzero Grok cache counters currently return `usage_incomplete` rather than invented fresh-input arithmetic. Provider-authored turn values, including Claude `num_turns`, never populate `turns_observed`; it remains null until Graphwing can derive an unambiguous server-observed boundary. Public jobs, polls, waits, and callbacks contain only the numeric aggregate and closed diagnostic—never raw native events, prompt/thought/response text, command lines, logs, paths, credentials, or provider payloads. Usage is sealed with the same terminal receipt authority as status, so post-terminal drift or authority loss fails closed.

`graphwing-implement-slice` preserves those two public fields in every writer receipt object builder, including fallback and correction writers. Rewst's native Data Object Storage library does provide Datastore Records operations; the earlier inventory incorrectly treated their absence from Graphwing's exported catalogs as absence from Rewst.

Phase 4.2 now builds one closed `graphwing-verified-outcome-v1` projection after each actual terminal leg and upserts it with `action.datastore.records.upsert`. The projection, candidate filtering, job-ID deduplication, usage rollups, callback binding, record key, and readback checks use Riftwing's native AST/object/array/aggregate/hash nodes rather than a monolithic Jinja/code-expression template. Storage is pinned to tenant scope, collection `graphwing_verified_outcomes_v1`, no TTL, and deterministic key `graphwing-outcome-v1:<WORKFLOW.runId>:implement-slice-terminal`. Replaying one run/stage replaces that row; a different Rewst run ID creates a distinct row. Successful upserts route through `action.datastore.records.get` using the same scope/collection/key before completion. Canonical JSON hashes require returned data to equal the projected record, and the returned key and version must equal the requested key and upsert version. The pending Herdr notification is deliberately excluded because its human wait can still acknowledge or time out; only that later terminal leg writes.

The record contains only Rewst workflow/run identity, terminal stage, route/profile and role/work-kind/class/size, sanitized agent receipt identity/status/failure, closed named-test evidence (`evidence_version`, accepted action `job_id`, configured test `name`, and terminal `status`), terminal-gate commit eligibility, `verified | spent_unverified | unverified`, separate writer/reviewer usage totals plus their deduplicated slice total, and a canned diagnostic. Every named-test callback must match its corresponding accepted action job ID; callback-supplied name/recipe text is never authoritative. Writer totals include every unique accepted primary, fallback, and correction job, even when its callback times out or fails; reviewer totals similarly include every unique accepted review attempt. Deduplication is by authoritative action job ID across aliases/retries, and any missing matching terminal usage makes the corresponding aggregate null with `usage_not_reported`. Riftwing's numeric aggregate is binary floating point, so durable `provider_cost_usd` aggregation is explicitly unavailable and remains null with `provider_cost_aggregation_unavailable`; decimal inputs such as 0.1 and 0.2 are never emitted as false-precision 0.30000000000000004. `verified` is reserved for `slice_complete` and `all_slices_complete`, including an initially empty frontier that reaches the latter through its terminal E2E gate; merely reaching `join_commit` cannot verify a later commit/push failure. The record never stores prompts, logs, paths, raw provider output, tokens, credentials, webhook secrets, or mutable local-file references. CTX and local receipts remain execution aids, not durable authority.

Datastore upsert failure, get failure, and readback mismatch route to terminal `transforms.regexReplace` nodes configured with an intentionally invalid RE2 lookahead. This uses the locally reviewed Rewst core-node contract—invalid regex configuration hard-fails the node—so those persistence faults cannot end as successful no-ops. The static node IDs/input codes distinguish the three machine-readable failure routes and contain no row data.

Local tests prove source topology, the exact native action/config contract, actual local Riftwing workflow lint compatibility, closed fields, deterministic-key reuse, callback/job binding, terminal-branch convergence, and exact readback/hash/version wiring. They do **not** prove tenant import, publication, a live durable write/readback, retention/access policy, or replacement-daemon historical reporting. Those require the later authorized publication and provider readback gate.

Terminal jobs and receipts expose `requested_effort`, `effective_effort`, `effort_source`, and a path-free `launcher_version` fingerprint. Session identity binds launcher/provider/model plus all four of those profile fields and, exactly when source is `route`, the closed route profile; resume, fallback, recovery, commit, merge, and code-off evidence rejects any drift. Before launch, the worker pins one canonical execution/profile/repository snapshot. Side-effectful staging and manifest preparation happen before the final exact per-job CAS, so trusted replacements are detected and left untouched. The final critical section rejects reentrant ordinary writes; terminal agent records then reject ordinary trusted replacement while a narrow guarded API may append callback bookkeeping. A queued writer therefore cannot replace success before callback delivery, and only a successful guarded transition can authorize that callback. Missing, replaced, alien-kind, or identity-drifted jobs fail closed; unrelated worker bookkeeping may survive when persisted before preparation. Every native and code-off agent record is stamped with canonical `kind: agent`; GET, wait, callback, resume, fallback, recovery, startup recovery, code-off consumption, and writer evidence require that positive classification. Missing, malformed, unknown, or known non-agent kinds are never inferred from record shape and fail closed. At code-off launch the server writes a content-addressed complete execution manifest and anchors its hash in the enclosing immutable event chain. It binds kind/role/slot/job attribution, launcher/provider/model, every effort field and launcher version, repo/branch/starting head, prompt and budget inputs, and launch/terminal native-session attribution. Freeze and judge consumers reconstruct the job, session identity, and receipt against those immutable launch and terminal manifests; missing, legacy, unknown, or coordinated drift fails closed. Graphwing hashes one opened launcher artifact, copies it into a sealed Linux descriptor, and hands that exact immutable descriptor to process creation. The fingerprint proves the exact selected wrapper artifact bytes executed; it does not attest the interpreter, PATH-resolved mise/shim, or provider package/tool bytes subsequently resolved by that wrapper. Repository provenance accepts a path-free allowlist short name (or closed internal code-off identity), the Phase 1 safe ASCII branch subset, and a full lowercase 40- or 64-hex Git object ID. The branch contract supports current Graphwing names such as `main`, `story/your-branch`, and `feature/model-routing`; it does not claim every Git-valid ref. Unsupported punctuation and Unicode, leading `-` or slash, trailing/repeated slash, `..`, `@{`, `@`, control/space, dot-leading or dot-trailing components, and any component ending `.lock` case-insensitively are rejected. Any duplicated top-level repo, branch, or starting head must exactly match the validated session identity, and public projection uses that identity rather than the mutable duplicates. Public surfaces intentionally keep the validated `starting_head` as repository provenance, but never trust launcher/model-supplied SHA/PR URL fields as authority. Otherwise they use closed typed identifiers and canned diagnostics, excluding prompts, logs, filesystem or credential paths, callback data, secrets/tokens, raw provider output, private URLs, launcher-authored summaries, and launcher-native effort tokens. Legacy interrupted jobs without a complete mutually corroborated execution profile return a schema-valid provenance failure with nullable, explicitly untrusted profile fields. Local manifests, job files, receipts, and process-local locks remain ephemeral coordination rather than durable or cryptographic authority; Phase 4.2's Rewst Datastore Records projection is the separate permanent terminal-outcome owner.

For ordinary work, `gitCommit` requires a valid `writer_job_id`; empty/omitted IDs and arbitrary staged-index commits are rejected. Explicit add paths are allowed only with validated writer evidence or the complete existing code-off experiment/final-verification gate.

Completed writer callbacks carry a `diagnostic-v1` object with a stable code, evidence
kind, and canned summary; pre-callback action/wait failures use compact diagnostic stage
receipts.
Every failover-eligible writer outcome remains
`provider_availability`, including a missing writer binary and structured provider
authentication or network evidence. `local_infrastructure` describes stack, port,
health, command, and configuration checks only; a missing non-writer executable is
`local_binary_missing`, not provider availability. The closed launcher failure taxonomy
and one-hop fallback gate are unchanged. Compact diagnostics and provider-recovery
evidence never copy callback data, paths, provider output, or traces.

Provider recovery is a new invocation decision, never an active-session decision.
After a successful one-hop fallback, a later manual/API invocation payload may set
`recovery_version: provider-recovery-v1` and supply the prior primary/fallback routes
and receipts. The recovery endpoint re-reads both terminal job files. Missing,
malformed, mismatched, nonterminal, or out-of-order evidence stops the run. A prior
successful fallback is the only recovery state both continuation paths carry forward;
a normal or recovered-primary run omits the marker.
`missing_binary` is rechecked with the same request-time resolver used by `agentRun`
preflight and worker launch; an explicit `GRAPHWING_<LAUNCHER>_BIN` remains
authoritative. Authentication, quota, rate, network, overload, and 5xx
failures stay on fallback unless the payload also supplies a distinct, later successful
normal-primary job receipt. The check never invokes a launcher or provider. Corrections
and review-nack resumes remain pinned to the successful session receipt selected at the
start of that invocation.

Lane 66 did not run or publish a live Rewst canary. Catalog fixtures assert the
`CTX.<objectBuilder alias>.<field>` recovery-switch path and fallback precedence, using
the alias convention recorded in issue-52 git history, but they do not prove how a live
tenant version will evaluate those expressions. Publish/canary verification remains a
separate runtime step.

### Sealed normalized attempt facts

`POST /v1/run/control/attempt-facts` is the additive v2 fact boundary. Its closed
request names one terminal agent `job_id` and the exact consumed Rewst authorization
identity persisted at launch: authorization ID plus the canonical launch-descriptor
hash. The daemon rereads the terminal job and accepts facts only when the complete
job/session/route/authorization/receipt projection still matches the process-local
terminal seal. After daemon replacement that seal is unavailable, so the endpoint
returns `authority_available: false` and null observed facts rather than trusting disk
or filling usage from the authorization envelope.

An available `normalized-attempt-facts-v2` response carries the authoritative session
identity and exact route execution profile, normalized terminal and failure facts,
nullable observed turns and provider cost, observed wall time and token counts, total
tokens, and the terminal receipt hash. `provider_cost_microusd_ceiling` is an exact
integer ceiling of provider-reported USD cost: every positive value below one
microdollar becomes one, while unknown cost remains null. The largest allowed value is
100,000,000,000, below JavaScript/Rewst exact-integer limits.

This operation does not read attempt history, aggregate budgets, substitute reservation
values, interpret progress, select routes, or return continue, handoff, restructure, or
terminal policy. The v1 validate-receipt, settle, authority-loss, and settled-receipt
surfaces remain compatibility-only until the approved v2 workflow cutover and live
zero-caller proof.

## Drive a PR to green

The active v1 call path is source-derived and pinned in the [issue #187 ownership and call-path baseline](notes/run-control-activation-recovery.md#issue-187-ownership-and-call-path-baseline). It remains compatibility behavior during the workflow-owned v2 migration; do not treat local run-control state as permanent authority.

```bash
python3 scripts/drive-pr.py 3526 --run-control-id rc1-... \
  --message "fix: address review findings [SC-110507]"
```

The owning Rewst lifecycle creates and retains the v2 run. The script accepts that
durable `run_control_id`, starts `graphwing-pr-drive`, and reads back its trace; it has
no route, aggregate-budget, evaluator-contract, review-polling, or local JSONL
authority. One Graph execution is a bounded DAG and can launch at most one writer;
the durable run may continue in a later execution. Correction admission and ordinal
come from immutable v2 history and budgets. A permanent tenant-scoped exact-head CAS
claim prevents concurrent or later duplicate writers for the same run and commit;
the claim is workflow state, not a local counter or attempt ledger. Every writer gets
fresh daemon descriptor-preparation facts, one read-back consumed authorization, and an exact
request body signed by the request-aware credential. Callback loss first polls and
waits for the canonical job; only confirmed authority loss records a full-envelope
charge. `--wait` defaults to the graph's longest reachable wait path
(`worst_case_run_seconds`) and refuses a smaller value. The script prints each run's
node trace; the run status endpoint returns only `completed`.

Native workflow policy owns `correct`, `await_exact_head_audit`,
`review_infrastructure_hold`, `verify`, and `park`. After commit and push, exact
local/remote head equality is recorded and the attempt is reconciled into Rewst v2
history. The invocation then ends at `await_exact_head_audit`; it never sleeps or
polls for review. A later invocation may correct again only with audit evidence for
that pushed head. Failed audit transport ends at `review_infrastructure_hold` without
consuming another writer attempt.

**Do not fire this workflow by webhook.** A webhook-triggered run never creates
`CTX.INPUT` at all, so `run_input` (which reads every field from `{{ CTX.INPUT.* }}`
once at the top of the graph) resolves empty and every downstream `{{ CTX.run_input.* }}`
does too; the run dies at `ghPrView` with no `number` on the query string. Nothing
reports it: `repo` still arrives, because `openapi.json` declares a default of
`riftwing` for that parameter, so the request looks half-correct. `fire-pr-drive.sh`
posts to the webhook and cannot work.

`run_slug` wraps the body as `{"input": {...}}`. A bare payload arrives as a manual
trigger with `body: {}` and fails the same silent way.

`--message` is not optional in spirit: `git_commit` rejects an empty message, and a run
that reaches the commit has already spent a full writer session. The graph defaults it
too, so a caller that forgets loses nothing.

The writer's task is not yours to write. Each attempt reads the bounded
`pr-policy-inputs-v1` fact packet. Native workflow nodes prefer exact-head PR Audit
records, fall back to Clean Code compatibility facts, keep only open below-bar
findings, deduplicate and order them, and separate policy, aggregate, transport, and
root-cause checks. The writer receives a serialized closed `pr-correction-task-v1`
with one fixed instruction, the exact head, actionable findings, and root checks.
Legacy daemon `needs_fix` and `brief` fields remain for compatibility but the active
graph does not read them. Stale reviews do not feed the writer.

Two node-output traps worth knowing before editing this graph. `action.graphwing` results
are exposed as `TASKS.<node>.data.<field>`, while a `transforms.objectBuilder` publishes
its configured alias at `CTX.<alias>`. An endpoint that nests its action payload under
`data` therefore needs double `.data.data`. A large action output can instead become an
artifact stub: `TASKS.checks.data.all_green` is unreadable because that node returns
~21KB, which is why the findings endpoint runs `gh pr checks` itself and returns a small
flat result.

`auto_merge` is per-run, defaults false, and is normalized once as explicit boolean human authorization. Native nodes classify raw PR state, draft, label names, review decision, mergeability/state, stable exact head, and normalized checks. `held`, `review-red`, `CI-red`, `pending`, `no-checks`, `malformed`, `unknown`, `conflicting`, and `closed` all terminate before the final callback wait; only `ready` checks out the confirmed head and runs one final named test. The earlier after-fix test remains advisory.
Only the final async job carries persisted `pr-merge-evidence-v1` repo/recipe/PR/run/head and clean start/end provenance. Its server-stamped `writer_evidence_mode` is `writer` after a fix and `initial_green` only when no writer ran; after-fix pins the successful writer/session job while initial-green has no writer reference.
For named scripts, tests, and RR recipes, supplying `response_webhook_url` forces async execution even when the catalog recipe defaults to sync, so a Graph wait always receives a terminal callback.
After the exact-head callback succeeds, the workflow rereads and confirms the same normalized facts before reporting ready and selecting `human_merge` or `request_merge`. Merge accepts that final persisted job ID only when its repo/recipe/PR/run/head and clean start/end provenance match. It then independently requires explicit authorization, an open non-draft PR, a stable head, `MERGEABLE`/`CLEAN`, no holds or blocking review, and nonempty terminal-passing GitHub checks. It invokes `gh pr merge --match-head-commit <sha>` once and never retries a moved head.
Code-off receipts remain independent issue-67 commit/push gates and cannot waive this final test. `graphwing-pr-status` applies the same remote-only native classification and performs no test or write effect. Legacy daemon readiness fields remain available for compatibility but neither active graph reads them.

## Other graphs

| Slug | When |
|---|---|
| `graphwing-verify-stack` | Stack down before e2e. Payload `{ "input": { "stack", "port" } }`; success includes stack/port diagnostics and action failures terminate with compact diagnostic stage receipts. |
| `graphwing-pr-status` | Read remote-only PR state with `{ "input": { "pr": 3624 } }` through an API start. Webhook starts remain disabled because they do not create `CTX.INPUT`. |
| `graphwing-pr-drive` | One bounded fix slice when remote checks/findings are red; start via manual/form/API run, not webhook. |

## Run a deterministic code-off

### Additive workflow-owned candidate lifecycle (policy v2)

The catalog contains a parallel v2 branch selected explicitly with `policy_version: code-off-policy-v2`; its activation gate currently permits initialization only. Omitting that field keeps the complete v1 workflow unchanged. The v2 branch builds and hashes one closed policy with native object-builder, hash, time, and filter nodes. That policy owns the ordered participant roster, fixed/random judge census, exact launcher identities, role effort, rubric and aggregation versions, classification, budgets, and its 2026-09-03 through 2026-10-03 approval window. An expired policy or an ineligible participant parks in the workflow before Graphwing resolves launcher bytes.

`codeOffV2Initialize` receives the policy and canonical hash plus the normal repository/task/test inputs. It does not accept a seed or blind map. Graphwing privately creates the seed, performs the cryptographic draw, pins exact launcher fingerprints and slot identities, creates only the two opaque author worktrees, and returns the commitment, public slot descriptors, hashes, and path-free workspace receipts. An exact untouched replay returns the same result. Policy drift, expiry after initialization, missing private seed authority, or missing/mutated worktrees parks without redraw, substitution, fallback, or a new launch.

`codeOffV2FreezeCandidate` binds one exact successful pinned author job to the
workflow's immutable freeze transition. It rejects ignored paths, unsafe links,
receipt drift, and mutation, then content-addresses the complete tree and returns
only closed hashes, counts, and repository facts. `codeOffV2TestCandidate` accepts
those exact frozen hashes plus its immutable test transition, materializes the
artifact in a disposable Git worktree, runs only the initialization-locked named
recipes, and returns normalized per-recipe pass/failure, timing, log-hash,
mutation, and receipt facts. Identical completed transition replays return the
stored result; changed payloads and second attempts fail closed. Neither operation
chooses parked, blind-ready, or another business stage. A receipt mismatch,
cryptographic/artifact corruption, path escape, ignored content, or mutation seals
the local candidate against later promotion without naming a workflow outcome.
The content-addressed seal binds experiment, slot, and rejection code, and its
hash is anchored in the verified local event chain. Missing or contradictory
state, event, or seal-artifact evidence fails closed. This uses the existing
immutable-record protocol; it does not claim protection from arbitrary deletion
by the same operating-system user. An interrupted test after its running marker
creates the same irreversible seal, and exact replay returns it without rerunning
the recipes.

The native `code-off-candidate-routing-v2` source topology owns ordered author
launches, terminal-success callback interpretation from the normalized `status`
and `job_id` receipt fields, one-attempt/no-retry gates, freeze/test transition
write-readback chains, both-candidate completion, and the workflow choice between
blind-ready and parked. Duplicate callback delivery addresses the same
deterministic transition; freeze revalidates the exact pinned job and local slot
idempotency prohibits a replacement. Freeze/test action failures flow through the
durable candidate-parked write and exact readback, while datastore write/readback
failure remains a hard-fail leaf.

The candidate topology remains behind a literal-false activation safety gate
because tenant Datastore Records have not proven atomic creation/version fencing.
The graph persists an author-launch transition, but `agentRun` intentionally does
not accept that transition binding and positively rejects v2 workspaces while the
gate is false. The topology is therefore an inactive source contract, not an
activated or live-operable author-effect path and not simulated CAS. The active
explicit v2 run still stops safely after initialization; the complete v1
author/freeze/test path remains unchanged and default. Activation, an `agentRun`
transition-binding contract, OpenAPI re-import, publication/readback, and live
canaries require separate direct approval.

### Existing v1 workflow

`graphwing-code-off` is one bounded DAG. Supply an unused lowercase `experiment_id`, an allowlisted repo at the exact clean `base_sha`, a 64-character lowercase hex seed, locked category/tags/source, the task, named tests, toolchain, budgets, and a commit message. Whole-value Graph interpolation may serialize tags, tests, toolchain, and budgets as bounded JSON strings; the server parses them before strict native validation. Callers never supply workspace paths; `agentRun` resolves only `{experiment_id, slot}`.

`code-off-draw-v1` uses the seed as an HMAC-SHA-256 key to rank the canonical pool and each judge's `[author-1, author-2]` order without modulo bias. The public commitment is `SHA-256("graphwing/code-off/seed-commitment/v1\\0" + seed)`; audit never returns the seed or a blind ordering before completion. There is no redraw, fallback, or cherry-picking.

The immutable record holds hash-chained events and content-addressed artifacts. Candidate/final recipes run identically with bytecode disabled in disposable Git worktrees; those worktrees are not OS isolation. Judges receive the locked task/category but no author identity. Finalization proves the frozen hash before a Git-safe target apply; ignored candidate paths, introduced identity leakage, or terminal candidate corruption park for cleanup, while infrastructure/persistence interruptions remain retryable. Audit is read-only.

### Pinned inference identity

`code-off-inference-profile-v1` binds each immutable slot to launcher/provider/exact model, effort provenance, and `launcher_version`, with slot/map hashes in the manifest. Exact supported Grok Node shebangs use a domain-separated composite SHA-256 over exact script and resolved Node bytes.

Stale approval, unproven identity, unavailable artifacts, or later profile/fingerprint drift parks or fails closed; no substitution, fallback, redraw, or partial pin is accepted.

### Aggregate economics

Finalization anchors one sanitized `code-off-economics-v1` artifact in final state/receipts. Authority-sealed usage has explicit missing-attempt and nullable-partial semantics; no prompts, logs, paths, seeds, or blind order leak.

Rewst alone owns permanent code-off economics through tenant-scoped native Datastore upsert/get with deterministic key, no TTL, canonical readback, and hard failure on mismatch. Local fixtures do not claim publication or live readback.

`approved-model-manifest-v2` currently makes the exact Grok 4.6, GPT-5.6 Sol/Terra, and Claude Sonnet/Opus/Fable 5 identities runnable and proven for code-off. Terra and Fable remain ineligible for ordinary jobs and enter code-off only through the immutable slot identity check. The bounded catalog inventory and canary evidence was obtained 2026-08-26; this is not continuous discovery. “Latest” means approved latest at policy creation, and the at-most-30-day policy expiration parks new runs pending an explicit reviewed update. There is no dynamic provider discovery, OpenCode dependency, alias resolution, substitution, fallback, or redraw. These catalog and fixture changes do not claim that this change ran or published a live Rewst code-off.

## Publish catalog graphs

Seat file `$GRAPHWING_HOME/rewst-install.json` (copy [`examples/rewst-install.example.json`](../examples/rewst-install.example.json)). Public OpenAPI URL comes from named-tunnel meta or `GRAPHWING_PUBLIC_URL`.

MCP token: `GRAPHWING_REWST_MCP_TOKEN`, or `mcp_bws_key` in that file plus `BWS_ACCESS_TOKEN`.

```bash
python3 scripts/publish_graphs.py --only all --no-run
```

Rewst rejects fan-in that is not `logic.join.*` and rejects unbounded cycles. One Graph run handles one slice; `kick_url` is generic catalog wiring whose webhook continuation is known broken under the current input mapping.

Repository checks are fixture/catalog proof only. See [HUMAN-LOOP.md](HUMAN-LOOP.md#rollout-verification-checklist) for the separate catalog/import, published, and live-canary evidence levels; this issue does not claim a publish or canary.

After any `openapi.json` change, new operation or renamed field, re-import the integration **before** publishing graphs:

```bash
python3 scripts/reimport_integration.py       # PUT sourceUrl, then publish, records the version
python3 scripts/publish_graphs.py --only implement-slice
```

Skipping this fails silently, which is the expensive part. Rewst builds every request from its own copy of the operation list, so a field the graph sends and the server reads is dropped on the floor when the integration does not declare it. SC-110290's third run lost `response_webhook_url` that way: the review ran synchronously, its wait node blocked on a callback nobody would send, and no layer reported anything.

Two parameter names to avoid: the connector treats a query parameter called `path` as the request URL path and overwrites the endpoint with its value. `fileHead` and `gitDiff` use `rel` for that reason.

## Shortcut from the seat

Legacy seat-side tracker calls use `scripts/sc`; Graphwing model jobs do not depend on it.

```bash
sc show SC-110290            # name, type, state, epic, branches, PRs
sc states 110290             # the states available on that story's board
sc mv 110290 "In Review"     # move it
sc comment 110290 "text"     # the receipt Graph still cannot post itself
```

Every failure path exits non-zero, including an unknown state name, so a caller cannot
mistake "did nothing" for "moved it".

## Deploy to the seat

systemd runs `$GRAPHWING_HOME/server.py`, a **copy**, not a symlink. `systemctl restart`
on its own comes back green while still serving the last copy, and nothing reports that
your change is not live.

```bash
scripts/deploy.sh
```

Runs the tests, copies the code files, restarts, then re-compares to prove the live file
is the one you built. `repos.json` and `stacks.json` are tracked in git but hold
placeholders; the real ones are seat config, so deploy never touches them.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 GRAPHWING_HOME=. python3 test_server.py
```

### Issue #188 PR 2 durable-controller boundary

The explicit `code-off-policy-v2` branch writes closed tenant Datastore Records for `code-off-experiment-v2` and deterministic `code-off-transition-v2` facts. Keys are experiment-scoped: `graphwing-codeoff-v2:<experiment_id>:experiment` and `graphwing-codeoff-v2:<experiment_id>:transition:<stage>:<ordinal>`. Each write is followed by same-key tenant `get` and exact data-hash, key, and version comparison; any failure/mismatch uses the graph's hard-failure node before another expensive effect. Initialization is downstream of a persisted workflow-generated transition and sends its exact ID and payload hash through the closed initialize request; Graphwing returns and seals that binding with its local idempotency authority. Records allowlist policy/workflow/experiment identity, seed commitment (never seed), selected public identities, receipt hashes, stage/reason/attempt IDs, and terminality; they exclude prompts, blind maps, logs, paths, tokens, and webhook material. Deterministic transition keys and transition-bound daemon idempotency make exact replay safe and reject a conflicting replacement effect. Mutable continuation is deliberately disabled because Datastore Records upsert/get has not proven atomic create/version fencing: transition immutability is not activated, and no read-then-upsert sequence is treated as CAS. A local initialization-authority failure writes only a durable parked fact and cannot reconstruct selection, judgment, or a winner. Source fixtures do not prove tenant atomicity, import, publication, retention, or live behavior.

### Issue #188 PR 3 candidate-stage boundary

The additive v2 freeze and candidate-test operations are transition-bound local
safety effects. Freeze consumes exactly one pinned successful author receipt and
content-addresses a complete-tree manifest and archive. Candidate test consumes
the exact frozen hashes, uses a disposable worktree and the initialization-locked
recipe specifications, and returns facts even when a named test fails. Business
failure never calls the v1 park/finalization policy. Non-bypassable receipt,
artifact, ignored-path, path-escape, mutation, and post-marker interruption
failures create one experiment/slot/code-bound, content-addressed local safety
seal anchored in the verified event chain while leaving the workflow to name the
durable outcome. V1 candidate operations reject v2 experiments before mutation
and cannot bypass that seal.

The graph contains the fully native, explicitly versioned candidate-stage source
topology and exact transition write/readback gates. It orders the two authors,
uses only normalized terminal callback status and job ID, prohibits retries,
joins two actionable candidate-test results, and durably selects blind-ready or
parked. Freeze/test action failures also reach that durable parked write/readback;
datastore persistence failures still hard-fail. Atomic tenant fencing is still
unproven, so a fixed false activation gate prevents this topology from launching
any v2 author, and Graphwing independently rejects a v2 `agentRun`. Author-launch
transition binding is not yet an active `agentRun` contract. This change claims
local source/fixture behavior only: no deployment, re-import, publication, tenant
readback, retention proof, or live canary was performed.
