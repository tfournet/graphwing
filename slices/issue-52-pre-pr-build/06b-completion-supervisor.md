# 06b-completion-supervisor: Reconcile every supervised workflow completion

**Behavior:** Every terminal Rewst run for a supervised Graphwing build reaches a durable Graphwing supervisor, which accepts a real terminal receipt, resumes recoverable work once, or parks an unhandled workflow fault with an actionable report.

**Seam:** The source-run completion event delivered by `trigger.workflowCompleted` to a named Graphwing reconciliation operation.

**Context:** Rewst now ships `trigger.workflowCompleted`. It can filter by source workflow ID or tag, terminal status, and execution mode. Its payload provides source workflow identity and tags, source run ID/status/timestamps, declared output, and public error presence. Rewst may still label a graph `completed` when it skipped the intended path, so the listener must receive every terminal status and Graphwing must require its own terminal receipt. A source run may fail before declaring a build ID; the launcher therefore records source run ID to build ID, event ID, and workflow version before monitoring begins. The listener calls Graphwing through the existing custom integration and API key. It does not self-kick the failed workflow or post to an expiring `action.wait.webhook` callback.

## Acceptance criteria

- [ ] A published `graphwing-build-completion-supervisor` workflow uses `trigger.workflowCompleted`, filters supervised source workflows by a dedicated tag or explicit IDs, listens to every terminal status in live mode, uses output context, and cannot listen to itself.
- [ ] The listener sends source workflow ID/slug/tags, source run ID/status/execution mode/timestamps, declared output, and public error presence to one named authenticated Graphwing operation; it contains no Graphwing API key, Rewst MCP token, or raw error/trace data.
- [ ] Launch registration durably maps Rewst source run ID to build ID, event ID, workflow ID/version, expected stage, and deadline before the run can be treated as supervised; duplicate source-run registration is idempotent and conflicting registration is rejected.
- [ ] Reconciliation deduplicates on source run ID plus stable listener identity, requires a matching launch record, and treats Rewst `completed` without the expected Graphwing terminal receipt as `workflow_fault` rather than success.
- [ ] Reconciliation returns an existing event receipt without repeating work, watches an existing local writer/test/reviewer job, relaunches a durably claimed job that never started, or retries the same unclaimed event ID; state/trace disagreement parks with files and stacks intact.
- [ ] Transient dispatch/network failure receives bounded retry, deterministic graph/schema/config failure does not retry unchanged, and the same workflow version plus failed node/error twice opens the circuit and parks; unknown failure gets one retry before parking.
- [ ] Every retry, park, and circuit-open outcome reports in the saved planning session with source run ID, workflow version, failed/last node when available, build stage, local job status, action taken, and exact next operator action; there is no idle human wait node.
- [ ] OpenAPI, unit tests, graph topology tests, completion-payload fixtures, and deterministic canaries cover `failed`, `timed_out`, `canceled`, successful-with-receipt, `completed`-without-receipt, duplicate delivery, early failure with no declared output, active local job, claimed-missing job, unclaimed event, conflicting state, transient retry, deterministic park, and circuit opening.
