# Published graphs

Hard-coded topology. Payload is the iteration. New file only when **edges** change.
Rewst publish requires fan-in on `logic.join.*` and no unbounded cycles. One Graph run remains bounded; the cataloged `kick_url` edge targets a fresh webhook run, but the current webhook-to-`CTX.INPUT` path is known broken and unusable.

How a human gets to a run: `docs/HUMAN-LOOP.md` and `docs/USING.md`. Slice map is `index` JSON in the worktree. `implement-slice` walks it serially. `pr-drive` keeps files on red.

`$GRAPHWING_INSTANCE`, `$GRAPHWING_STATUS_REPO`, and the webhook secret placeholder are replaced at publish from environment/install configuration.
Public OpenAPI URL comes from named-tunnel meta (`cloudflared-meta.json`) or `GRAPHWING_PUBLIC_URL`.
riftwing rings graphwing OIDC, no Rewst secrets.

The `pr-status` webhook is disabled, while the `implement-slice` and `pr-drive` webhooks require authentication. Current webhook runs do not create `CTX.INPUT`, so webhook starts and chained continuations remain unusable for all three graphs. Use a manual/form or `/workflows/{slug}/run` start with `{ "input": {...} }` for `implement-slice` and `pr-drive`; only the API start is proven for `pr-status`.

`.github/workflows/pr-drive-doorbell.yml` has two independent defects: the current webhook-to-`CTX.INPUT` mapping is broken, and it sends `pr` while `pr-drive` consumes `pr_number`. It is not rollout proof or a current operator start; do not infer readiness from the workflow's presence.

```bash
GRAPHWING_REWST_MCP_TOKEN=... python3 scripts/publish_graphs.py --only pr-drive
```

Copy `examples/rewst-install.example.json` to `$GRAPHWING_HOME/rewst-install.json` and fill org/instance IDs. Do not commit the filled file.

| Slug | Input | Shape |
|---|---|---|
| `graphwing-run-control-authorize` | `record_key`, `server_instance_challenge` | inert foundation: native durable record load/upsert/readback/canonical SHA-256 → exact found/key/version/hash and descriptor/challenge gate → short-lived challenge-bound issued-to-consumed KV CAS; no existing launch graph calls it yet |
| `graphwing-run-control-transition` | `expected_pointer_version`, `from_logical_revision`, `operation_id`, `operation_type`, `owner_workflow_run_id`, `run_control_id`, `target_logical_revision`, `target_state`, `target_state_record_key`, `target_state_sha256`, `transition_delta`, `transition_request_sha256` | dormant stable → pending → content-addressed Record materialize/readback/hash → stable transition with deterministic recovery and fail-closed pending state |
| `graphwing-run-control-initialize` | `budgets`, `evaluator_contract_sha256`, `initial_route`, `root_identity` | dormant closed-root-identity and immutable-budget initialization through an exactly pinned transition version |
| `graphwing-run-control-state` | `branch`, `callback_binding_sha256`, `candidate_route`, `endpoint`, `exact_request_body_sha256`, `handoff`, `head_sha`, `launcher_fingerprint`, `next_envelope`, `permission_profile`, `repository`, `run_control_id`, `task_sha256` | dormant propagated-ID verified-state evaluation, continuation/handoff guard, current challenge, and one persisted reservation; no launch edge |
| `graphwing-run-control-reconcile` | `authority_loss_reason`, `kind`, `receipt`, `run_control_id` | dormant exact receipt or full-envelope authority-loss reconciliation through a pinned transition |
| `graphwing-run-control-consume-authorization` | `attempt_id`, `authorization_id`, `launch_descriptor_sha256`, `server_instance_challenge`, `state_record_key`, `state_sha256` | dormant exact authorization issued-to-consumed KV CAS helper |
| `graphwing-run-control-consume` | `run_control_id` | dormant verified reservation consumption and durable consumed-state update; no launch edge |
| `graphwing-verify-stack` | `stack`, `port` | stackStatus → one scalar port parameter; success retains `diagnostic-v1`, while action failures end in compact diagnostic stage receipts |
| `graphwing-implement-slice` | `repo`, `branch`, `index`, `ticket`, `commit_message`, `test`, `iters_left`, `class`, `work_kind`, `size`, `ac_count`, `seams`, `kick_url`, `kick_token`, `e2e`, `recovery_version`, `prior_primary_route`, `prior_primary_receipt`, `prior_fallback_route`, `prior_fallback_receipt`, `fresh_primary_receipt` | frontier → route → fileHead → optional later-invocation `provider-recovery-v1` evidence recheck → one-hop writer/fallback → named test → opposing review → complete → commit → push; active corrections stay pinned, red parks with files, and recovery/catalog coverage is fixture-only |
| `graphwing-pr-drive` | `repo`, `pr_number`, `attempt`, `max_attempts`, `auto_merge`, `kick_url`, `kick_token`, `test`, `class`, `size`, `work_kind`, `prompt`, `commit_message` | remote state → at most one writer fix + advisory test → commit/push → fresh head checkout → one persisted final named test → optional strict evidence-ID/match-head merge; all refusal paths terminate safely |
| `graphwing-pr-status` | `pr` | API-started remote-only classification; webhook starts remain disabled; performs no named-test job or git write |
| `graphwing-code-off` | `experiment_id`, `repo`, `base_sha`, `seed`, `category`, `tags`, `category_source`, `prompt`, `tests`, `toolchain`, `budgets`, `commit_message` | immutable deterministic prepare → two frozen/tested candidates → three blind judged receipts → aggregate → final test/hash proof → commit/push gate; six exact policy-current identities are eligible (Terra/Fable only in code-off), while expiry parks new runs for review; worktrees are not OS isolation; no discovery, alias, redraw, fallback, cycle, or merge |

These are repository catalog claims, not Rewst import, publish, or live-canary receipts. The code-off policy's bounded catalog inventory and canary evidence was obtained 2026-08-26; it is not continuous provider discovery and does not claim this change ran or published a live Rewst code-off. There is no visual-proof graph in this catalog.
