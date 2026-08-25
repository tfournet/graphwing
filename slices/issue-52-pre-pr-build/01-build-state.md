# 01-build-state: Persist an idempotent pre-PR build

**Behavior:** Repeated calls for one pre-PR build resume from durable state and cannot duplicate or reorder side effects.

**Seam:** The durable build event transition.

**Context:** `graphwing-pre-pr-build` will run as many bounded Rewst invocations. State belongs under `GRAPHWING_HOME`, not in the product worktree. A build ID identifies the work. An event ID identifies one requested transition. The state must survive service restart and retain the story, repository, branch, head, writer session, stack identities, declared verification instructions, reviewer receipts, screenshot rounds, job budget, and current stage. It must not store credentials, webhook secrets, or unredacted tokens. The existing slice-index loader currently normalizes ticket entries; preserve the routing and verification metadata this story adds rather than dropping it during rewrites.

## Acceptance criteria

- [ ] Creating a new build records a versioned state document and returns its build ID, stage, and next allowed action.
- [ ] Repeating an event ID with the same canonical payload returns the recorded receipt without rerunning the transition.
- [ ] Reusing an event ID with different input is rejected as an idempotency conflict and leaves state unchanged.
- [ ] A lease prevents two callers from advancing the same build concurrently; an expired lease can be recovered without losing the last verified boundary.
- [ ] An unexpected branch head, worktree identity, or incompatible state version parks the build with a specific reason instead of guessing.
- [ ] Restarting the Graphwing service reloads the build and can report the same stage, receipts, writer session, stacks, evidence rounds, and remaining budget.
- [ ] State writes are atomic, bounded, redact secret-shaped values, and never place runtime state in git.
- [ ] OpenAPI and tests cover valid transitions, invalid transitions, replay, conflicting replay, lease exclusion, expiry recovery, restart recovery, and metadata preservation.
