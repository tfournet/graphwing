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

`agentRun` accepts the closed launcher-neutral effort vocabulary `default | low | medium | high | max`. In Phase 1 the conservative capability table advertises only behavior established by the current adapters: Codex Sol (and code-off-only Terra) accepts `default` or `high`, both with effective `high`; Claude Opus/Sonnet (and code-off-only Fable) and Grok 4.6 accept only `default`. Other known values fail with `unsupported_effort`; unknown values fail with `bad_effort`. A supplied value has source `explicit`; omission records requested `default` with source `launcher_default`. `route` is reserved for internally attributable route wiring and is not emitted by direct `agentRun` in this phase.

Terminal jobs and receipts carry requested/effective effort, source, and a path-free launcher executable fingerprint. Session identity binds launcher/provider/model plus requested effort, effective effort, effort source, and launcher version, so resume, recovery, fallback, commit, and merge evidence fail closed on any execution-profile drift. The launcher bytes are copied from one opened artifact into a sealed Linux descriptor; those exact hashed bytes are passed to process creation. Public surfaces intentionally retain the validated `starting_head` as repository provenance, but use only closed identifiers and canned diagnostic summaries otherwise: they exclude launcher/model-supplied SHA/PR URL values, prompts, logs, filesystem or credential paths, callback data, secrets/tokens, raw provider output, private URLs, launcher-authored summaries, and launcher-native effort tokens. Legacy restart records without complete corroborated profile evidence fail closed with nullable untrusted profile fields.

`gitCommit` requires a valid `writer_job_id` for every ordinary commit and commits only attributed writer paths plus explicit paths through an isolated index. The only no-writer exception is an existing complete `codeoff_experiment_id`/`final_verification_hash` gate. Empty or omitted writer IDs never authorize an arbitrary staged-index commit, and explicit adds require one of those evidence modes. `gitCommit` never runs `git add -A`; an empty commit is a failed slice. Keep files and do not push.

## Retry and park

Resume only the native session identity recorded in a successful Graphwing receipt. Send compact failing test names or must-fix review items, not reviewer reasoning. Never switch launchers implicitly.

A timeout without a verdict, exhausted retry budget, repeated red suite, or repeated review rejection parks the slice with files intact. Discard is the only operation allowed to wipe work.

## Operational boundaries

- `diagnostic-v1` is a compact receipt contract, not a trace dump. `provider-recovery-v1` may reconsider a prior fallback only at a later invocation boundary from persisted evidence; active corrections remain pinned.
- Daemon jobs, logs, caches, session files, and local receipts are replaceable runtime corroboration, never durable authority. Rewst data storage is the future permanent receipt store; Phase 1 adds no Rewst persistence.
- Phase 1 makes direct agent effort validation and execution identity real, but does not add Phase 2 launcher command propagation. Review effort and graph-node effort remain unwired until later phases.
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
