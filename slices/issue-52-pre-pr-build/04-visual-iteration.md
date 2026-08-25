# 04-visual-iteration: Iterate visual feedback through planning

**Behavior:** The user can refine a visual change through an open-ended planning conversation while each code update remains a bounded, recoverable workflow call.

**Seam:** The visual-feedback event transition.

**Context:** Human visual feedback is expected design iteration, not pass/fail and not a reviewer strike. The planning session controls the conversation. Each feedback event resumes the saved writer session, preserves the preview stack, runs changed-area checks and typecheck, drives the browser scenario, runs automated visual review, captures verified screenshots, posts a numbered Shortcut comment, and reports the new candidate in planning. There is no fixed turn ceiling. Turn 20 is an advisory checkpoint. The user must choose continue, reframe, split, or park before more code is written. Reframe returns to planning; split records separate work; park preserves the branch, state, stack, and evidence. "Design ready" starts finalization. A final-gate fix that could change the UI returns to this loop.

## Acceptance criteria

- [ ] A visual-feedback event records the user's exact notes, resumes the same writer context, and rejects stale or duplicate conflicting feedback without changing files.
- [ ] Each turn reruns changed-area tests and typecheck, the declared live browser scenario, and automated UI/accessibility review before presenting a new candidate.
- [ ] Each turn publishes a new verified screenshot set and explained Shortcut comment, preserving prior rounds as history.
- [ ] The planning session receives the exact URL, navigation instructions, what changed in the round, what to inspect, and the screenshots.
- [ ] User feedback starts another design turn and never consumes a test, reviewer, or retry strike.
- [ ] At turn 20 the workflow pauses and requires continue, reframe, split, or park; continue preserves all context and does not impose a later hard turn limit.
- [ ] A design-ready event advances to final gates, while reframe, split, park, and explicit stop each produce distinct durable states and receipts.
- [ ] If final checks require a fix that can affect visible output, the workflow returns to visual iteration and cannot commit until the resulting candidate has completed its required human path.
