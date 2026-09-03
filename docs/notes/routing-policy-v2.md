# routing-policy-v2 candidate

## Status and authority

- `normal-v1` remains the active writer policy exposed by `POST /v1/slice/route`.
- `routing-policy-v2-candidate` is benchmark-only, unpromoted, and non-production. It is available only through the centralized Python candidate table/function and is not an OpenAPI route. Nothing in this catalog may select it for real work, and it cannot authorize execution.
- Candidate route profiles deliberately fail the existing `route-execution-profile-v1` parser because their route version is not executable. Promotion requires the Phase 8 benchmark and a separately reviewed activation change.
- No graph, tenant integration, published workflow, launcher, or provider is changed or invoked by this candidate.

## Evidence gate for task-shape inputs

The retained routing inputs are the objective, closed-schema fields `class`, `work_kind`, size floor (`size`), `ac_count`, and `seams`. Provider-free fixtures prove a deterministic effect for each.

`ac_count` and `seams` are typed counts, not coercible values. The normal, fallback, and recovery requests accept a JSON integer or a plain ASCII decimal string (Rewst renders graph inputs as strings) inside `0–99` and `0–20` respectively. JSON booleans, floats, signed or zero-padded text, digit separators, whitespace, and non-ASCII digits are rejected as `bad_ac_count` / `bad_seams` before any route, launcher, or authorization step. `routing_policy_v2_candidate()` applies the same closed parser and raises `ValueError` rather than benchmarking a value the public route would refuse.

The repository contains a historical `normal-v1` characterization and durable outcome plumbing, but it does not yet contain the representative, repeated, programmatically verified benchmark corpus required by Phase 8. The sanitized historical snapshot is not joined to task-shape fields and therefore cannot demonstrate that another field changes verified outcomes.

Consequently, `closed_contract`, `existing_failing_test`, and `external_verification` are omitted from the request schema and rejected as unexpected fields. They may be reconsidered only when a durable benchmark artifact proves a deterministic routing effect. Red test state is never a routing input and cannot escalate a model, effort, size, or review.

## Central candidate writer policy

`ROUTING_POLICY_V2_WRITER_TABLE` and `routing_policy_v2_candidate()` are the single source for all 27 work-kind/class/effective-size combinations. The candidate emits `policy_version: routing-policy-v2`, `policy_status: benchmark_candidate`, its non-executable route version, and a human-readable reason.

| Work kind | Shape | Candidate writer profile |
|---|---|---|
| Go coding | Mechanical S/M with a known 0–1 seams | Claude Sonnet 5, medium |
| Go coding | Mechanical L, unknown/multiple seams, visual, or sensitive | GPT-5.6 Sol, high |
| TypeScript coding | Mechanical S/M with a known 0–1 seams | Claude Sonnet 5, medium |
| TypeScript coding | Mechanical L, unknown/multiple seams, visual, or sensitive | Claude Opus 5, high |
| Research/operations | Nonsensitive S/M with a known 0–1 seams | Grok 4.6, medium |
| Research/operations | L, unknown/multiple seams, or sensitive | Grok 4.6, high |

The existing AC-count size bump remains. A known seam count greater than one, or an omitted seam count, selects the conservative L candidate. This candidate does not add Aider or a Luna alias because neither is an approved ordinary execution profile in the current catalog.

## Writer and reviewer separation

Writer effort comes from the active writer catalog or the candidate writer table. Reviewer effort is class-based and independent of writer effort: Normal review effort is `medium`; sensitive review effort is `high`. Reviewer providers remain opposing-provider, and sensitive work still receives two distinct reviewers.

A candidate Mechanical S route is review-free only when its effective size remains S and the caller supplies a known seam count of 0 or 1. Unknown or multiple seams conservatively select L and one review. This is an objective eligibility rule; it does not claim that a subjective “closed contract” was measured.

Review execution remains independently bounded by the closed review request/identity contract. The defaults are 12 turns and 200 seconds, while validated caller bounds remain 1–30 turns and 30–600 seconds. Review receives the existing bounded prompt/ticket material, diff hash/content boundary, and test evidence supplied by the workflow; no writer turn or wall budget is reused as a review limit.

## Verification level

Tests enumerate all 27 candidate combinations, task-shape boundaries, review-free and multi-seam cases, normal/sensitive reviewer effort, native routed-review command boundaries, active-policy preservation, and fail-closed candidate execution evidence. Expected writer profiles, effective sizes, budgets, reviewer identities, and complete reason strings are hard-coded in the tests and are never recomputed from `server.py`. Adversarial fixtures cover unknown extras, malformed and contradictory counts, booleans supplied as integers, range boundaries, coordinated reviewer-effort drift, and candidate substitution at the agent, review, fallback, recovery, and resume boundaries, asserting rejection before any launcher resolution or spawn. Request/OpenAPI field-set parity for the normal, fallback, and recovery schemas is asserted directly against the runtime allowlists. These are local source and provider-free fixture claims only; they do not establish benchmark superiority, publication, tenant parity, or live readiness.
