# Published graphs

Hard-coded topology. Payload is the iteration. New file only when **edges** change.
Rewst publish requires fan-in on `logic.join.*` and no unbounded cycles. One Graph run remains bounded; the cataloged `kick_url` edge targets a fresh webhook run, but the current webhook-to-`CTX.INPUT` path is known broken and unusable.

How a human gets to a run: `docs/HUMAN-LOOP.md` and `docs/USING.md`. Slice map is `index` JSON in the worktree. `implement-slice` walks it serially. `pr-drive` keeps files on red.

`$GRAPHWING_INSTANCE`, `$GRAPHWING_STATUS_REPO`, the routing-policy workflow/version pins, and the webhook secret placeholder are replaced at publish from environment/install configuration.
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
| `graphwing-routing-policy` | `class`, `work_kind`, `size`, `ac_count`, `seams` | deterministic native validation, one-step effective size, normal-v1-compatible writer/budget/opposing-review policy, and complete workflow-owned `route-execution-profile-v2` output; published before and exact-pinned by both primary consumers |
| `graphwing-run-control-authorize` | `record_key`, `run_control_id`, `server_instance_challenge`, `state_version` | compatibility-only foundation helper; cataloged but absent from the source subworkflow graph and the `pr-drive` publication chain |
| `graphwing-run-control-transition` | `commit_kind`, `expected_pointer_version`, `from_logical_revision`, `operation_id`, `operation_type`, `owner_workflow_run_id`, `previous_record_sha256`, `run_control_id`, `state_version`, `target_logical_revision`, `target_ordinal`, `target_pointer`, `target_pointer_sha256`, `target_record`, `target_record_key`, `target_record_sha256`, `target_state`, `target_state_record_key`, `target_state_sha256`, `transition_delta`, `transition_request_sha256` | active v1 stable → pending → content-addressed Record materialize/readback/hash → stable transition with deterministic recovery and fail-closed pending state |
| `graphwing-run-control-initialize` | `budgets`, `evaluator_contract_sha256`, `initial_route`, `root_identity`, `state_version` | compatibility v1 plus immutable v2 history initialization; the operator driver no longer constructs these policy inputs |
| `graphwing-run-control-state` | `branch`, `callback_binding_sha256`, `candidate_route`, `decision_only`, `endpoint`, `exact_request_body_sha256`, `handoff`, `head_sha`, `launcher_fingerprint`, `next_envelope`, `owner_workflow_run_id`, `permission_profile`, `repository`, `run_control_id`, `state_version`, `task_sha256`, `verified` | v2 native authoritative policy with gaming-before-size precedence, exact-route continuation/efficiency state, conservative projection, and durable terminal outcomes; v1 evaluation remains isolated |
| `graphwing-run-control-reconcile` | `attempt_job_id`, `authority_loss_reason`, `authorization_identity`, `kind`, `owner_workflow_run_id`, `progress`, `receipt`, `reserved_envelope`, `run_control_id`, `state_version` | active v1 child of `pr-drive`: exact receipt or full-envelope authority-loss reconciliation through `transition` |
| `graphwing-run-control-consume-authorization` | `attempt_id`, `authorization_id`, `launch_descriptor_sha256`, `server_instance_challenge`, `state_record_key`, `state_sha256` | active v1 authorization child; publisher pin resolution targets this six-input graph from `consume`'s `authorize` node |
| `graphwing-run-control-consume` | `descriptor_preparation`, `expires_at`, `issued_at`, `run_control_id`, `state_version` | v2 exact descriptor construction and issue/readback/consume/readback CAS returning one complete authorization; the v1 child path remains compatible |
| `graphwing-verify-stack` | `stack`, `port` | stackStatus → one scalar port parameter; success retains `diagnostic-v1`, while action failures end in compact diagnostic stage receipts |
| `graphwing-implement-slice` | `repo`, `branch`, `index`, `ticket`, `commit_message`, `test`, `iters_left`, `class`, `work_kind`, `size`, `ac_count`, `seams`, `kick_url`, `kick_token`, `e2e`, `recovery_version`, `prior_primary_route`, `prior_primary_receipt`, `prior_fallback_route`, `prior_fallback_receipt`, `fresh_primary_receipt` | frontier → exact-pinned routing-policy child → v2 primary writer → named test → v2 opposing review → complete → commit → push; every correction copies the successful session/profile; v1 fallback and recovery branches remain compatibility surfaces for later migration |
| `graphwing-pr-drive` | `repo`, `pr_number`, `auto_merge`, `test`, `class`, `size`, `work_kind`, `prompt`, `commit_message`, `run_control_id` | bounded DAG over one durable v2 run: native policy chooses `correct`, `await_exact_head_audit`, `review_infrastructure_hold`, `verify`, or `park`; correction ordinal and admission come from immutable run-control history/budgets; a tenant exact-head CAS claim and one consumed signed authorization fence each writer; a successful push reconciles durably and stops for fresh exact-head audit evidence |
| `graphwing-pr-status` | `pr` | API-started remote-only classification; webhook starts remain disabled; performs no named-test job or git write |
| `graphwing-code-off` | `experiment_id`, `repo`, `base_sha`, `seed`, `category`, `tags`, `category_source`, `prompt`, `tests`, `toolchain`, `budgets`, `commit_message` | immutable deterministic prepare → two frozen/tested candidates → three blind judged receipts → aggregate → final test/hash proof → commit/push gate; six exact policy-current identities are eligible (Terra/Fable only in code-off), while expiry parks new runs for review; worktrees are not OS isolation; no discovery, alias, redraw, fallback, cycle, or merge |

These are repository catalog claims, not Rewst import, publish, or live-canary receipts. The code-off policy's bounded catalog inventory and canary evidence was obtained 2026-08-26; it is not continuous provider discovery and does not claim this change ran or published a live Rewst code-off. There is no visual-proof graph in this catalog.

See [the issue #187 ownership and call-path baseline](../docs/notes/run-control-activation-recovery.md#issue-187-ownership-and-call-path-baseline) for the mechanically checked reachability, daemon classification, migration rule, and approval gates.
