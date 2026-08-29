# Human loop (operator lock)

How an engineer turns a GitHub issue into a PR. Rewst Graph owns topology. Graphwing exposes bounded local operations.

Use your normal editor or terminal. Do not reconstruct operating rules from an old chat transcript.

## Surfaces

- GitHub Issues hold the approved idea and acceptance criteria. The implementation queue is the slice map in the worktree.
- Pre-platform notes live outside this repo.
- Native `codex`, `claude`, and `grok` launchers must already be installed. Graphwing never installs or wraps them.
- Herdr is optional. Session `graphwing`, tab `graph`, is a deterministic job dashboard. Do not use it as a planner or chat seat.

## Flow

1. Grill the idea and record the accepted behavior on its GitHub issue.
2. Stamp class (`mechanical`, `visual`, or `sensitive`), work kind (`go_coding`, `typescript_coding`, or `research_ops`), and size floor (`S`, `M`, or `L`).
3. Structure the approved work into a slice map in the app worktree. Graph does not invent slices.
4. Start `graphwing-implement-slice` manually/form-side or through `/workflows/{slug}/run` with `{ "input": {...} }`; the current webhook input path is known broken.
5. Run named local tests from `tests.json`. On red, files stay on disk and the run parks.
6. After the map is empty, run the named smoke or e2e recipe before opening a PR.
7. Open the PR and verify independent review. Merge only after the final persisted named-test job and a fresh, stable, nonempty terminal-green GitHub state both pass.

## Slice map

- Cut on behavior, not token budget.
- Prefer a demoable vertical slice with 3-8 testable acceptance criteria and one primary seam.
- `blocked_by` edges form a DAG. Frontier means an unblocked open `build` ticket.
- A `decision` ticket parks the workflow until the engineer resolves it.
- Run one writer at a time on a branch.
- The writer sees the ticket file, not the planning transcript.

## One slice

1. `gitCheckout` selects or creates the branch.
2. `sliceRoute` chooses an explicit native launcher, provider, and model from work kind.
3. `agentRun` starts one bounded native session and returns a receipt with structured session identity.
4. `testRun` runs the allowlisted ticket and story recipe.
5. Opposing-provider review gates commit when required.
6. Graphwing attributes and stages only the writer-produced paths after a valid receipt. Graph commits and pushes only after green gates.

Go uses Codex, TypeScript uses Claude, and research/operations uses Grok. Mechanical S skips model review. Other work gets one opposing-provider review; sensitive work gets two. Missing launchers, model mismatches, untraceable resumes, missing session identity, and invalid receipts fail closed.

`agentRun` and `reviewRun` accept the closed launcher-neutral effort vocabulary `default | low | medium | high | max`. Phase 2 propagates the validated effective value at the shared native command boundary: Codex Sol (and code-off-only Terra) accepts all five values, with `default` effective as `high`; Claude Opus/Sonnet (and code-off-only Fable) accepts all five values and omits `--effort` only for effective `default`; Grok 4.6 accepts `default | low | medium | high`, with requested `default` effective as native `high`. Grok `max` remains unsupported. Other known values fail with `unsupported_effort`; unknown values fail with `bad_effort`. A supplied value has source `explicit`; omission records requested `default` with source `launcher_default`. `route` remains reserved for internally attributable route wiring and is not emitted by direct calls.

The sanitized local contract probe used Codex CLI 0.150.1 (`-c model_reasoning_effort=<value>` before the exec payload), Claude Code 2.1.250 (`--effort <low|medium|high|xhigh|max>` as a top-level session option), and Grok 1.0.5 (`grok agent --reasoning-effort <none|minimal|low|medium|high|xhigh|max> ... stdio`; the option belongs before `stdio`). The embedded `grok-4.6` catalog declares native default `high` and advertises `low | medium | high | xhigh`. Graphwing exposes the intersection with its launcher-neutral vocabulary: `low | medium | high`, plus requested `default` mapped to `high`; it exposes neither `xhigh` nor parser-only `max`. Writer and review fixtures assert the same provider/model/effort profile with distinct write versus read-only permission modes; no fixture invokes a provider.

Review acceptance now creates a separate immutable `review-execution-v1` identity and snapshot; it does not classify a review as an agent job. The identity binds kind, launcher/provider/model, requested/effective/source effort, launcher fingerprint, repo/branch/head, prompt and diff hashes, budget/turn limits, and the read-only permission profile. Review effort provenance is review-specific and accepts only `explicit | launcher_default`; coordinated mutation to the agent-only `route` source is noncanonical at identity, worker, receipt, runtime, poll, and wait boundaries. For an async review, acceptance seals both the canonical snapshot and exact wrapper bytes in process-local Linux descriptors and registers that authority under the server-authored job ID. The worker never reconstructs authority from the owner-writable job or snapshot files, so even a coordinated rewrite of those files and their hash cannot select a different launch. Async callers poll only `GET /v1/review/jobs/{id}` or wait with `POST /v1/review/jobs/wait`; agent-job endpoints continue to reject review records. Public jobs, receipts, waits, and callbacks contain only the closed sanitized projection, never prompts, paths, logs, tokens, or provider output. Public lifecycle timestamps are exact server-authored second-resolution UTC values; malformed or hostile values make the record noncanonical and are never projected. After canonical acceptance, every ordinary `write_job` call against the existing review returns `False` under the per-job lock, including a structurally equal object or one whose keys were reordered; this is a strict no-op that does not invoke the atomic writer, replace the file, or touch persisted review bytes. The guarded queued-to-running transition creates one exact closed record with its server-authored `started_at`, canonical null operational fields, and no unknown keys, then seals the canonical bytes and digest in process-local authority. Final transition rereads and validates that entire running value—identity, timestamps, receipt/error/webhook nulls, and exact key set—against the sealed CAS before committing a terminal receipt. Callback bookkeeping is a webhook-only guarded update against the exact committed terminal revision. Any drift leaves the replacement byte-for-byte untouched and emits no callback; stale accepted state is never restored.

Terminal jobs and receipts carry requested/effective effort, source, and a path-free launcher executable fingerprint. Session identity binds launcher/provider/model plus requested effort, effective effort, effort source, and launcher version, so resume, recovery, fallback, commit, merge, and code-off evidence fail closed on any execution-profile drift. Workers pin the canonical execution/profile/repository snapshot before launch. Side-effectful staging and manifest preparation run before the final exact per-job CAS, so trusted replacements are detected and left untouched. The final critical section rejects reentrant ordinary writes; after commit, terminal agent records reject ordinary trusted replacement while a narrow guarded API may append callback bookkeeping. Thus a queued writer cannot replace success before callback delivery, and only the guarded transition can authorize that callback. Missing, replaced, alien, or identity-drifted records fail closed, while unrelated worker bookkeeping may survive when persisted before terminal preparation. Native and code-off agent records carry canonical `kind: agent`; every agent trust boundary positively classifies that kind instead of inferring authority from shape. Missing, malformed, alien, and known non-agent kinds fail closed. When a code-off slot launches, the server writes a content-addressed complete execution manifest and anchors its hash in the enclosing immutable event chain. It binds canonical kind/role/slot/job attribution, launcher/provider/model and all effort fields, launcher version, repo/branch/starting head, prompt and budget inputs, and launch/terminal native-session attribution. Freeze and judge consumption reconstruct job, session identity, and receipt against those immutable launch and terminal manifests; missing, legacy, unknown, or drifted evidence fails closed even when mutable surfaces were changed coherently. The launcher bytes are copied from one opened artifact into a sealed Linux descriptor; those exact hashed bytes are passed to process creation. The fingerprint proves the exact selected wrapper artifact bytes executed; it does not attest the interpreter, PATH-resolved mise/shim, or provider package/tool bytes subsequently resolved by that wrapper. Repository provenance accepts a path-free allowlist short name (or closed internal code-off identity), the documented Phase 1 safe ASCII branch subset, and a full lowercase 40- or 64-hex Git object ID. This subset supports current Graphwing values such as `main`, `story/your-branch`, and `feature/model-routing`; it is deliberately not every Git-valid branch. It rejects unsupported punctuation and Unicode, leading `-` or slash, trailing/repeated slash, `..`, `@{`, `@`, controls/space, dot-leading or dot-trailing components, and case-insensitive `.lock` suffix components. Duplicated top-level repo, branch, and starting-head evidence must agree exactly with session identity, and public projection is sourced from the validated identity. Public surfaces intentionally retain the validated `starting_head` as repository provenance, but never accept launcher/model-supplied SHA/PR URL fields as authority. Otherwise they use only closed identifiers and canned diagnostic summaries, excluding prompts, logs, filesystem or credential paths, callback data, secrets/tokens, raw provider output, private URLs, launcher-authored summaries, and launcher-native effort tokens. Legacy restart records without complete corroborated profile evidence fail closed with nullable untrusted profile fields.

`gitCommit` requires a valid `writer_job_id` for every ordinary commit and commits only attributed writer paths plus explicit paths through an isolated index. The only no-writer exception is an existing complete `codeoff_experiment_id`/`final_verification_hash` gate. Empty or omitted writer IDs never authorize an arbitrary staged-index commit, and explicit adds require one of those evidence modes. `gitCommit` never runs `git add -A`; an empty commit is a failed slice. Keep files and do not push.

## Retry and park

Resume only the native session identity recorded in a successful Graphwing receipt. Send compact failing test names or must-fix review items, not reviewer reasoning. Never switch launchers implicitly.

A timeout without a verdict, exhausted retry budget, repeated red suite, or repeated review rejection parks the slice with files intact. Discard is the only operation allowed to wipe work.

## Operational boundaries

- `diagnostic-v1` is a compact receipt contract, not a trace dump. `provider-recovery-v1` may reconsider a prior fallback only at a later invocation boundary from persisted evidence; active corrections remain pinned.
- Daemon jobs, logs, caches, session files, local manifests, receipts, and process-local per-job locks are replaceable, ephemeral runtime coordination and corroboration, never durable authority. Active async review authority exists only in the accepting process's sealed descriptors and private registry. A replacement daemon cannot resume that review: startup closes it as non-authoritative, emits no review callback from stale disk evidence, and poll/wait returns a sanitized `409 review_authority_unavailable`. Rewst-owned continuation is still required for resumability. Terminal polling validates and allowlists the exact closed receipt rather than returning stored JSON verbatim. The lock prevents trusted in-process daemon writers from racing a terminal compare-and-transition; it is not an OS integrity or cryptographic boundary against an arbitrary same-user filesystem writer that ignores the protocol. Rewst data storage is the future permanent receipt store; Phase 2 adds no Rewst persistence.
- Phase 2 makes native writer and reviewer command propagation real. Rewst graph-node effort remains intentionally unwired until Phase 3; no graph topology changed here.
- Code-off is eligible only while its at-most-30-day embedded approval is current and every exact immutable slot identity is proven; Fable and Terra are code-off-only, not ordinary launcher choices. Expiry or provenance mismatch parks rather than discovering, aliasing, substituting, falling back, or redrawing. Git worktrees are not OS isolation.
- Final named-test merge evidence is server-stamped with `writer_evidence_mode: initial_green | writer`. Initial-green requires no writer ID; writer mode requires the complete matching writer/session profile. Missing, unknown, legacy, or contradictory modes fail closed.
- PR status is remote-only. Merge requires the persisted final named-test job bound to repo/PR/run/head and clean start/end, then fresh open, non-draft, stable-head, merge-ready state with a nonempty terminal-green GitHub check set.
- There is no visual proof scope in this catalog. Preserve issue-52 visual exclusions; a named recipe is not screenshot/browser evidence.
- Local green tests do not claim Rewst import parity, publication, live readiness, or a canary.

## Rollout verification checklist

Run local checks from the catalog root. They neither start services nor contact Rewst/providers.

**Fixture proof** — deterministic local fixtures, including issues 65-68 behavior; no tenant/runtime claim:

```bash
PYTHONDONTWRITEBYTECODE=1 GRAPHWING_HOME=. python3 test_server.py
```

**Catalog/import proof** — compile, parse OpenAPI and every graph, verify DAG/fan-in/catalog/writeback gates, then inspect the diff. This proves the source catalog is internally consistent; an actual tenant import still needs a fresh authorized readback.

```bash
PYTHONPYCACHEPREFIX=/tmp/graphwing-pyc python3 -m py_compile server.py scripts/publish_graphs.py scripts/reimport_integration.py test_server.py
python3 - <<'PY'
import json
from pathlib import Path
for path in [Path("openapi.json"), *sorted(Path("graphs").glob("*.json"))]:
    json.loads(path.read_text())
PY
PYTHONDONTWRITEBYTECODE=1 GRAPHWING_HOME=. python3 -m unittest \
  test_server.DispatchTests.test_graphs_fan_in_targets_are_joins \
  test_server.DispatchTests.test_catalog_docs_inputs_and_publish_all_writeback_cover_every_graph \
  test_server.DispatchTests.test_rollout_docs_name_proof_levels_commands_and_safety_limits \
  test_server.DispatchTests.test_implement_slice_initial_availability_fallback_topology \
  test_server.DispatchTests.test_implement_slice_recovery_is_only_a_later_initial_boundary \
  test_server.DispatchTests.test_pr_drive_has_one_final_evidence_leg_and_no_merge_bypass \
  test_server.DispatchTests.test_pr_status_reports_remote_only_states_without_side_effects \
  test_server.CodeOffTests.test_codeoff_graph_is_bounded_waited_fanned_in_and_terminal_gated
git diff --check
```

**Published proof** — only an authorized run of `python3 scripts/reimport_integration.py` followed by `python3 scripts/publish_graphs.py --only all --no-run`, plus fresh provider workflow/version/spec readback, can establish it. Saved IDs alone are insufficient.

**Live canary proof** — requires a deployed source-matching API/OpenAPI, published/read-back graph, and an actual bounded nonvisual run trace. Active units alone are not proof. The code-off policy's bounded catalog inventory and canary evidence was obtained 2026-08-26, but is not continuous discovery or proof that this change ran/published a live Rewst code-off; expiration parks new runs pending policy review. No visual canary is in scope.

## Do not

- Install or manage model launchers through Graphwing.
- Add a planner or chat seat to Herdr.
- Implement in the grill session.
- Let a writer's provider review its own work.
- Escalate model or size because tests are red.
- Run full e2e on every slice.
- Parallelize writers on one story branch.

How to fire a run: [USING.md](USING.md).
