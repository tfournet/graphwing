# 02-mechanical-path: Drive a mechanical change to a PR-ready branch

**Behavior:** One resumable workflow takes a bounded mechanical change from its approved ticket through real pre-PR checks, focused review, commit, and push.

**Seam:** The `graphwing-pre-pr-build` stage switch for the mechanical path.

**Context:** This is the first complete path through the new workflow. Each Rewst invocation is bounded and starts by claiming the durable build event. Multi-ticket selection and PR creation live above this workflow. The planning record declares named fast and integration recipes. Graph validates every name before the writer starts. Changed-path rules may add mandatory recipes but cannot remove declared ones. The current `riftwing-local-gates` recipe is only a pre-commit hook and must not be presented as unit, type, or integration coverage. The writer keeps one session. Every change receives an independent behavior-and-test micro-review. Findings are correction turns until the review is clean or the total job budget expires. Budget exhaustion preserves files and returns control to planning. Only finalization may commit and push.

## Acceptance criteria

- [ ] The published workflow can be called with a build ID and event ID, reads durable state, and routes to the next allowed mechanical stage.
- [ ] Preflight rejects missing or unknown recipes, missing routing stamps, dirty conflicting worktrees, and invalid branch state before launching a writer.
- [ ] The writer receives only the approved ticket and verification instructions, keeps one resumable session, stages its own files, and never commits or pushes.
- [ ] Every declared fast check runs to a completed verdict; queue acknowledgements, missing verdicts, and skipped commands cannot pass the gate.
- [ ] The independent behavior-and-test reviewer checks scope, behavior, RED evidence where required, production-path coverage, and the declared integration seam.
- [ ] Reviewer findings resume the same writer, rerun affected deterministic checks, and rerun the reviewer until clean or budget exhaustion; provider silence is retried and is not treated as a finding.
- [ ] Final integration runs in a separate clean stack and cannot substitute results from a developer or preview stack.
- [ ] Commit and push occur once only after final checks and review are clean; replay returns the existing PR-ready receipt and never creates a second commit or push.
