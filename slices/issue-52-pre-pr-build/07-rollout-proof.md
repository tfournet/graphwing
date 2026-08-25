# 07-rollout-proof: Prove the published pre-PR workflow

**Behavior:** Operators can trust the live `graphwing-pre-pr-build` workflow because mechanical, visual, and sensitive canaries prove its gates and recovery behavior against the published version.

**Seam:** The rollout evidence receipt for the published workflow.

**Context:** `implement-slice` remains available during rollout. Publishing is not proof. Re-import the custom integration after OpenAPI changes, publish the new workflow, and compare the live graph and operation schema with the catalog. Use disposable allowlisted proof repositories and synthetic data for canaries. The visual canary uses the real writer, preview stack, browser, automated reviewer, Shortcut attachment path, planning callback, repeated human feedback, final clean integration stack, and merge teardown. The sensitive canary uses no real credentials or tenant data but must exercise both independent reviews. Preserve run IDs, node traces, test receipts, review identities, screenshot file IDs, state transitions, commits, and teardown receipts.

## Acceptance criteria

- [ ] Catalog tests validate graph topology, bounded runs, stage routing, wait-node verdicts, idempotent events, and failure paths for every new operation.
- [ ] The custom integration is re-imported after OpenAPI changes, the workflow publishes without validation errors, and live operation and graph hashes match the catalog.
- [ ] A mechanical canary starts from no state, survives at least one correction turn, runs real fast and clean integration checks, receives behavior/test acknowledgement, and produces exactly one pushed commit.
- [ ] A visual canary performs at least two human feedback rounds, preserves one preview stack and writer context, posts legible explained screenshots to Shortcut each round, completes final clean-stack gates, and returns to visual review after an injected UI-changing final fix.
- [ ] The turn-20 checkpoint is tested deterministically without requiring twenty paid writer runs, and all four actions produce the documented state transition.
- [ ] A sensitive canary proves Terra and Grok review the same final diff, neither can be skipped, `no_verdict` differs from a finding, and no real credential or tenant data enters evidence.
- [ ] PR merge tears down the preview stack exactly once; PR close without merge prompts planning and leaves the stack untouched until the recorded choice.
- [ ] Operator docs describe the new workflow, declared verification instructions, visual interaction, evidence comments, recovery, budgets, and rollout status while clearly retaining `implement-slice` during the trial.
