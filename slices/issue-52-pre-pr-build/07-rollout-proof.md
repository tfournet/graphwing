# 07-rollout-proof: Prove the published pre-PR workflow

**Behavior:** Operators can trust the live `graphwing-pre-pr-build` workflow because mechanical, visual, and sensitive canaries prove its gates and recovery behavior against the published version.

**Seam:** The rollout evidence receipt for the published workflow.

**Context:** `implement-slice` remains available during rollout. Publishing is not proof. Re-import the custom integration after OpenAPI changes, publish the new workflow, and compare fresh live operation/schema and graph readback with the committed catalog. Use disposable allowlisted proof repositories and synthetic data for canaries. Ticket 07 and ticket 03 evidence belong on GitHub issue #52 only: the visual canary uses the real writer, one preview stack, browser, automated reviewer, GitHub issue comment/artifact path, planning callback, repeated human feedback, final clean integration stack, and merge teardown. The sensitive canary uses no real credentials or tenant data but must exercise both independent reviews. Preserve run IDs, node traces, post-transition readbacks, test receipts, server-authored review identities, screenshot/a11y artifact identities, state transitions, commits, and teardown receipts. No final PR or rollout-complete claim precedes the complete census and cleanup readback.

## Acceptance criteria

### Direct-harness correction (local catalog only)

- Graphwing launches Codex, Claude, or Grok Build directly from the configured
  provider/model route. Codex starts pending, then atomically binds its emitted thread ID to the durable job after exact session metadata validation. Hermes is not an agent, reviewer, writer, async-job,
  or Herdr-resume transport.
- Claude and Grok use preassigned canonical UUID sessions. Codex starts
  pending and binds its emitted `thread.started.thread_id` atomically to the
  exact durable job and session metadata record; latest/time lookup is never
  an authority. Native terminal output is adapter-validated before a normalized
  receipt exists: missing, malformed, stale, nonzero, mixed identity, or
  fallback output is terminal before a verdict can count.
- Graphwing performs no harness retry or provider fallback. The published
  Rewst graph owns the visible primary-failure → failover branch and supplies a
  distinct second direct provider/model attempt.
- Catalog proof covers Codex, Claude, and Grok command construction, direct
  Herdr resumes, and rejection of stale, fallback, multiple, and missing
  structured receipts. This correction does not publish, canary, deploy, open
  a PR, or close ticket 07.

- [ ] Catalog tests validate graph topology, bounded runs, stage routing, wait-node verdicts, idempotent events, and failure paths for every new operation.
- [ ] The custom integration is re-imported after OpenAPI changes, the workflow publishes without validation errors, and deterministic catalog hashes match fresh post-publish API readback; unreadable/error/mismatch creates no parity receipt and blocks canaries/final PR.
- [ ] Every canary/transition receipt binds its current canary run ID and commit/change identity and has a separate post-transition acceptance readback; old or duplicate transition receipts cannot satisfy the census.
- [ ] A mechanical canary starts from a named disposable branch in a fresh clone with an empty build-state store, survives at least one correction turn, runs real fast and clean integration checks, receives behavior/test acknowledgement, and remote merge-base/ref readback proves exactly one pushed commit with no force-push.
- [ ] A visual canary performs at least two human feedback rounds on one writer session/preview, posts legible explained screenshots and a11y evidence to GitHub issue #52 each round, binds every artifact to served SHA/preview URL+port/session/round/evidence identity, completes final clean-stack gates, and returns to visual review after an injected UI-changing final fix with fresh post-change artifacts.
- [ ] A sensitive canary proves server-authored Terra/Grok provider/model/role receipts bind the same final diff and evidence identity, neither can be skipped, missing/`no_verdict` blocks without becoming a finding, and no real credential or tenant data enters evidence.
- [ ] The deterministic turn-20 proof uses the live checkpoint decision path; one real canary proves advisory continuation has no later ceiling.
- [ ] PR merge tears down the preview stack exactly once with independent unit/port readback; PR close without merge records the exact expected pane and leaves the preview running with durable pending state until the recorded choice.
- [ ] Operator docs describe the new workflow, declared verification instructions, visual interaction, GitHub evidence comments, recovery, budgets, receipt census, and open rollout status while clearly retaining `implement-slice` during the trial. No credential store, persistence object, service/job, backfill, operator command, deployment protocol, production repair/adoption, or final PR is added.
