# routing-v1 baseline

## Provenance and scope

- Catalog base: `9f7a178c8491f1ea7ac7dab14f5e6856254c45a8` (`origin/main` at characterization time).
- Route contract: `normal-v1`.
- Evidence level: local source, provider-free fixtures, and one immutable committed snapshot of sanitized categorical/numeric observations. This is not provider, Rewst publication, tenant-import, or live-canary proof.
- No launcher or provider was invoked to produce this note or its fixtures.
- Paths use `$WORKTREE` for the isolated catalog checkout and `$GRAPHWING_HOME` for the replaceable local Graphwing seat. They are placeholders, not host paths.

## Frozen route behavior

The characterization fixture sends JSON through authenticated `POST /v1/slice/route` dispatch. It enumerates all 27 `work_kind` / class / size-floor combinations and compares each complete response, including writer and reviewer identities, declared efforts, effective size, turn limit, and wall budget.

| Work kind | Writer launcher | Provider | Model | Declared effort |
|---|---|---|---|---|
| `go_coding` | `codex` | `openai` | `gpt-5.6-sol` | `high` |
| `typescript_coding` | `claude` | `anthropic` | `claude-opus-5` | `default` |
| `research_ops` | `grok` | `xai` | `grok-4.6` | `default` |

Reviewer order is provider-dependent and opposing-provider:

| Writer provider | Reviewer 1 | Reviewer 1 effort | Reviewer 2 | Reviewer 2 effort |
|---|---|---|---|---|
| `openai` | `claude` / `anthropic` / `claude-sonnet-5` | `default` | `grok` / `xai` / `grok-4.6` | `default` |
| `anthropic` | `codex` / `openai` / `gpt-5.6-sol` | `high` | `grok` / `xai` / `grok-4.6` | `default` |
| `xai` | `codex` / `openai` / `gpt-5.6-sol` | `high` | `claude` / `anthropic` / `claude-opus-5` | `default` |

| Class / size floor | Effective size when counts are omitted | Max turns | Wall budget (seconds) | Reviewers |
|---|---:|---:|---:|---:|
| mechanical / S | S | 10 | 120 | 0 |
| mechanical / M | M | 30 | 300 | 1 |
| mechanical / L | L | 50 | 600 | 1 |
| visual / S | M | 30 | 600 | 1 |
| visual / M | L | 50 | 900 | 1 |
| visual / L | L | 50 | 900 | 1 |
| sensitive / S | S | 10 | 180 | 2 |
| sensitive / M | M | 30 | 600 | 2 |
| sensitive / L | L | 50 | 900 | 2 |

Current size-bump behavior is also frozen through the public dispatch surface, with complete response assertions for every case:

- `ac_count >= 6` bumps one size step, capped at L; `ac_count <= 5` does not.
- Visual work bumps one step when `seams` is omitted or equals `1`.
- Visual work does not receive the seam bump when `seams` is `0` or `2`; an AC-count bump still applies.
- Explicit concurrent cases use visual S plus `ac_count = 6` with `seams` both omitted and equal to `1`. Both conditions are true, but the result is M, proving current one-step-only behavior rather than two accumulated steps.

## Declared effort is dead metadata

At this base:

- `sliceRoute` emits writer and reviewer effort strings.
- `AgentRunRequest` has `additionalProperties: false` and no `effort` property. `/v1/agent/run` therefore returns `400 unexpected_fields` for `effort`.
- `ReviewRunRequest` has no `effort` property and does not close additional properties. The request parser also has no unknown-field rejection.
- A bounded provider-free `POST /v1/review/run` fixture supplies a unique effort sentinel. The endpoint accepts the request, silently drops the sentinel, and calls a mocked native-review boundary without effort. The fixture forbids queued review execution and fails if the schema closes, the request is rejected, the sentinel is persisted/returned, or effort is passed into the review boundary.
- No `/v1/agent/run` or `/v1/review/run` graph-node config contains an `effort` payload field.

These are characterization facts, not desired behavior. Any intentional wiring or contract change requires a new baseline version.

## Immutable sanitized point-in-time observation

This section records an **ephemeral local diagnostic observation**, not an authoritative metrics store. The Graphwing daemon/seat is replaceable; its jobs, logs, usage, and caches may disappear normally. Their disappearance is expected and does not invalidate the immutable table committed here. Permanent normalized metrics, workflow outcomes, and audit records belong only in Rewst data storage in later phases; Phase 0 makes no production change toward that design.

Snapshot selection was fixed before aggregation:

- Observation date: 2026-08-28.
- Inclusion window: `created_at >= 2026-08-26T00:00:00Z` and `created_at < 2026-08-28T00:00:00Z`.
- Included records: launcher exactly `codex`, `claude`, or `grok`, with parseable `created_at` in that fixed window.
- Source read once: replaceable local `job.json` diagnostics below `$GRAPHWING_HOME/jobs`.
- Sanitization allowlist: launcher, provider, model, terminal status, and numeric provider-native usage fields only.
- Excluded: individual timestamps, job/session identifiers, paths, URLs, prompts, receipts other than numeric usage values, logs, provider response text, and credentials.

The committed values below—not the mutable/non-versioned local source—are the Phase 0 snapshot baseline.

### Categorical snapshot (103 records)

| Category | Immutable counts |
|---|---|
| Launcher | Claude 53; Codex 23; Grok 27 |
| Provider | Anthropic 44; OpenAI 23; xAI 17; unrecorded 19 |
| Model | Claude Fable 5: 12; Claude Opus 5: 21; Claude Sonnet 5: 20; GPT-5.6 Sol: 8; GPT-5.6 Terra: 15; Grok 4.6: 27 |
| Terminal status | completed 85; failed 18 |
| Provider-native usage present | 18 |
| Provider-native usage missing | 85 |

Fable and Terra observations are not ordinary `normal-v1` route choices. Missing provider or usage values are unknown, never inferred as a named provider or zero usage.

### Provider-native usage snapshot (18 records)

This is **provider-native usage**, not normalized usage. Field names preserve the native receipt categories and are not asserted to have cross-provider-equivalent semantics. There are nine Claude records and nine Grok records with numeric usage; there are no Codex records with persisted provider-native usage in this snapshot. That absence is a coverage gap, not evidence of zero Codex consumption.

Estimator for the summary table: sort the values ascending, use the first and last values for min/max, and select zero-based index `round((n - 1) * p)` for P50 and P90 (`p = 0.5` or `0.9`, Python 3 ties-to-even rounding). Each populated field has `n = 9`.

| Launcher | Native numeric field | Min | P50 | P90 | Max |
|---|---|---:|---:|---:|---:|
| Claude | input tokens | 4 | 10 | 12 | 20 |
| Claude | cache creation input tokens | 17,045 | 23,750 | 29,321 | 30,649 |
| Claude | cache read input tokens | 52,112 | 162,869 | 228,624 | 360,465 |
| Claude | output tokens | 414 | 1,627 | 3,001 | 3,069 |
| Grok | input tokens | 1,663 | 18,625 | 24,383 | 36,177 |
| Grok | cache creation input tokens | 0 | 0 | 0 | 0 |
| Grok | cache read input tokens | 52,736 | 86,528 | 118,912 | 124,032 |
| Grok | output tokens | 484 | 950 | 1,291 | 1,947 |
| Grok | reasoning tokens | 343 | 741 | 869 | 1,523 |
| Grok | total tokens | 85,032 | 96,483 | 122,948 | 127,235 |

The following sanitized sorted vectors are the committed aggregate representation from which the table can be recomputed. They cannot be joined back to individual records because row relationships, identifiers, and timestamps were intentionally removed.

```text
claude.input_tokens = [4, 4, 8, 8, 10, 10, 10, 12, 20]
claude.cache_creation_input_tokens = [17045, 20199, 21483, 22517, 23750, 24704, 29019, 29321, 30649]
claude.cache_read_input_tokens = [52112, 62602, 126686, 131205, 162869, 170100, 191400, 228624, 360465]
claude.output_tokens = [414, 433, 1161, 1333, 1627, 2467, 2577, 3001, 3069]
grok.input_tokens = [1663, 2423, 3134, 17408, 18625, 19005, 22829, 24383, 36177]
grok.cache_creation_input_tokens = [0, 0, 0, 0, 0, 0, 0, 0, 0]
grok.cache_read_input_tokens = [52736, 62592, 65152, 71552, 86528, 90368, 94336, 118912, 124032]
grok.output_tokens = [484, 780, 875, 902, 950, 1087, 1193, 1291, 1947]
grok.reasoning_tokens = [343, 587, 628, 640, 741, 807, 846, 869, 1523]
grok.total_tokens = [85032, 86712, 90860, 91264, 96483, 108726, 112104, 122948, 127235]
```

Full raw-source reproducibility is intentionally not claimed: retaining raw local diagnostics would conflict with sanitization and replaceable-seat architecture. The fixed selection rules and estimator explain the snapshot; the committed categorical table and sorted numeric vectors are its immutable evidence.

## Verification commands

Run from `$WORKTREE`:

```bash
git status --short --branch
git rev-parse HEAD
git rev-parse origin/main
git merge-base HEAD origin/main

PYTHONDONTWRITEBYTECODE=1 GRAPHWING_HOME=. python3 -m unittest \
  test_server.DispatchTests.test_slice_route_current_matrix_is_frozen \
  test_server.DispatchTests.test_declared_effort_is_dead_for_agent_and_review_requests
PYTHONDONTWRITEBYTECODE=1 GRAPHWING_HOME=. python3 test_server.py

PYTHONPYCACHEPREFIX=/tmp/graphwing-pyc python3 -m py_compile \
  server.py scripts/publish_graphs.py scripts/reimport_integration.py test_server.py
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
