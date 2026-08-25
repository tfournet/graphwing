# 06-pr-lifecycle: Close preview stacks from PR lifecycle events

**Behavior:** Preview resources remain available through PR review and are reclaimed safely when the PR lifecycle ends.

**Seam:** The PR lifecycle event transition for a completed build.

**Context:** The pre-PR workflow returns a pushed PR-ready branch but does not create or merge a PR. A workflow above it reports PR-opened, PR-merged, or PR-closed events using the same build ID and unique event IDs. The preview stack stays running after final screenshots and while the PR is open. Merge tears it down automatically. Close without merge asks the planning session whether to keep or stop it. Final integration stacks are separate and may be torn down as soon as their receipts are durable. Duplicate lifecycle events must be harmless. An unexpected or partially failed teardown must report exactly what remains.

## Acceptance criteria

- [ ] A PR-opened event records the PR identity and leaves the preview stack available at its existing URL.
- [ ] A PR-merged event stops the preview stack, verifies its ports and units are gone, and records a durable teardown receipt.
- [ ] Replaying PR-opened or PR-merged events returns the existing result without restarting or stopping resources twice.
- [ ] A PR-closed-without-merge event prompts the planning session to keep or stop the preview stack and performs no silent teardown.
- [ ] Clean integration stacks stop after their final evidence is recorded and never disturb the preview stack.
- [ ] Failed or partial cleanup parks with the surviving stack, ports, and next operator action reported in planning and Graph logs.
