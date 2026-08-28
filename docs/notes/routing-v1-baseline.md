# routing-v1 baseline

## Provenance and scope

- Catalog base: `9f7a178c8491f1ea7ac7dab14f5e6856254c45a8` (`origin/main` at characterization time).
- Route contract: `normal-v1`.
- Evidence level: local source, fixtures, and sanitized numeric aggregates only. This is not provider, Rewst publication, tenant-import, or live-canary proof.
- No launcher or provider was invoked to produce this note.

## Frozen route behavior

The characterization fixture enumerates all 27 `work_kind` / class / size-floor combinations and asserts the complete writer and reviewer identities, declared effort, effective size, turn limit, and wall budget.

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

Current size-bump behavior is also frozen:

- `ac_count >= 6` bumps one size step, capped at L; `ac_count <= 5` does not.
- Visual work bumps one step when `seams` is omitted or equals `1`.
- Visual work does not receive the seam bump when `seams` is `0` or `2`; an AC-count bump still applies.
- Concurrent AC and seam conditions still produce only one step.

## Declared effort is dead metadata

At this base:

- `sliceRoute` emits writer and reviewer effort strings.
- `AgentRunRequest` has `additionalProperties: false` and no `effort` property.
- `/v1/agent/run` returns `400 unexpected_fields` for an `effort` field.
- No `/v1/agent/run` or `/v1/review/run` graph-node config contains an `effort` payload field.

The fixture asserts all four facts without changing production behavior, OpenAPI, or graphs.

## Sanitized local aggregate evidence

The aggregation read only `job.json`, selected an explicit allowlist of categorical and numeric fields, and emitted counts and min/P50/P90/max values. It did not emit prompts, job/session IDs, logs, paths, URLs, receipt text, tokens used for authentication, or provider responses.

Latest 30 native-model job records by `created_at`:

- Window: `2026-08-27T04:32:12+00:00` through `2026-08-27T21:59:54+00:00`.
- Launcher counts: Claude 16, Codex 6, Grok 8.
- Provider counts: Anthropic 16, OpenAI 6, xAI 8.
- Model counts: Claude Fable 5: 4; Claude Opus 5: 4; Claude Sonnet 5: 8; GPT-5.6 Sol: 3; GPT-5.6 Terra: 3; Grok 4.6: 8. Fable and Terra records are code-off evidence, not ordinary-route choices.
- Terminal status counts: completed 28, failed 2.
- Declared max turns (min/P50/P90/max): 10 / 30 / 30 / 50.
- Declared wall budget seconds: 300 / 300 / 600 / 900.
- Observed wall seconds: 15 / 131 / 358 / 778.
- Persisted normalized usage coverage: 0 of 30. Missing usage is unknown, not zero.

The local store contained 18 earlier records with numeric native usage (`2026-08-26T04:04:50+00:00` through `2026-08-26T05:51:13+00:00`):

| Launcher (records) | Numeric field | Min | P50 | P90 | Max |
|---|---|---:|---:|---:|---:|
| Claude (9) | fresh input tokens | 4 | 10 | 12 | 20 |
| Claude (9) | cache creation input tokens | 17,045 | 23,750 | 29,321 | 30,649 |
| Claude (9) | cache read input tokens | 52,112 | 162,869 | 228,624 | 360,465 |
| Claude (9) | output tokens | 414 | 1,627 | 3,001 | 3,069 |
| Grok (9) | fresh input tokens | 1,663 | 18,625 | 24,383 | 36,177 |
| Grok (9) | cache creation input tokens | 0 | 0 | 0 | 0 |
| Grok (9) | cache read input tokens | 52,736 | 86,528 | 118,912 | 124,032 |
| Grok (9) | output tokens | 484 | 950 | 1,291 | 1,947 |
| Grok (9) | reasoning tokens | 343 | 741 | 869 | 1,523 |
| Grok (9) | total tokens | 85,032 | 96,483 | 122,948 | 127,235 |

The 18-record numeric sample has no Codex usage records and the latest-30 sample has no persisted normalized usage. These are baseline coverage gaps, not evidence of zero Codex consumption.

## Commands used

```bash
git -C /home/tim/work/gw-routing-v2 status --short --branch
git -C /home/tim/work/gw-routing-v2 rev-parse HEAD
git -C /home/tim/work/gw-routing-v2 rev-parse origin/main
git -C /home/tim/work/gw-routing-v2 merge-base HEAD origin/main

PYTHONDONTWRITEBYTECODE=1 GRAPHWING_HOME=. python3 -m unittest \
  test_server.DispatchTests.test_slice_route_current_matrix_is_frozen
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

Aggregate-only runtime command (the tuple indexes are the explicit allowlist described above):

```bash
GRAPHWING_HOME=/home/tim/.graphwing python3 - <<'PY'
import json, os
from collections import Counter
from datetime import datetime
from pathlib import Path
T=lambda v: datetime.fromisoformat(str(v).replace('Z','+00:00')) if v else None
def Q(v):
 v=sorted(v); return None if not v else (v[0],v[round((len(v)-1)*.5)],v[round((len(v)-1)*.9)],v[-1])
r=[]
for p in Path(os.environ['GRAPHWING_HOME'],'jobs').glob('*/job.json'):
 try: d=json.loads(p.read_text()); c=T(d.get('created_at'))
 except (OSError,json.JSONDecodeError,ValueError): continue
 if not c or d.get('launcher') not in {'codex','claude','grok'}: continue
 try: s,f=T(d.get('started_at')),T(d.get('finished_at'))
 except ValueError: s=f=None
 n=((d.get('transport_provenance') or {}).get('native_receipt') or {})
 r.append((c,d['launcher'],d.get('provider') or 'unrecorded',d.get('model') or 'unrecorded',d.get('status') or 'unrecorded',d.get('max_turns'),d.get('run_budget_seconds'),(f-s).total_seconds() if s and f else None,n.get('usage') if isinstance(n,dict) else None))
a=sorted(r)[-30:]
print('recent',len(a),a[0][0].isoformat(),a[-1][0].isoformat())
for i,k in ((1,'launcher'),(2,'provider'),(3,'model'),(4,'status')): print(k,dict(sorted(Counter(x[i] for x in a).items())))
for i,k in ((5,'turns'),(6,'budget'),(7,'wall')): print(k,Q([x[i] for x in a if isinstance(x[i],(int,float))]))
print('recent_usage_coverage',sum(isinstance(x[8],dict) for x in a),len(a))
u=[x for x in r if isinstance(x[8],dict)]
print('usage_records',len(u),min(x[0] for x in u).isoformat(),max(x[0] for x in u).isoformat())
for launcher in sorted({x[1] for x in u}):
 lane=[x for x in u if x[1]==launcher]; print('usage_lane',launcher,len(lane))
 for k in ('input_tokens','cache_creation_input_tokens','cache_read_input_tokens','output_tokens','reasoning_tokens','total_tokens'): print(k,Q([x[8][k] for x in lane if isinstance(x[8].get(k),(int,float))]))
PY
```
