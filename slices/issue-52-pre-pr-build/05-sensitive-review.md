# 05-sensitive-review: Gate sensitive work with independent review

**Behavior:** Auth, permission, tenant-isolation, credential, privacy, and data-loss changes cannot become PR-ready until independent behavior and security reviews both acknowledge the final diff.

**Seam:** The sensitive specialist-review chain.

**Context:** Sensitive is the highest routing class. Opus writes sensitive work. Terra reviews behavior and tests. Grok reviews security and tenant isolation. Both reviews are focused micro-reviews and both gate finalization. A sensitive change may also be visible; `needs_visual_proof` remains a separate verification requirement and triggers the visual evidence path. Reviewer findings are correction turns. Any correction changes the diff, so all applicable reviewers rerun. Provider timeout, connection loss, or silence is `no_verdict`, not a negative opinion. No reviewer receives another reviewer's hidden reasoning.

## Acceptance criteria

- [ ] Sensitive routing launches Opus as writer, Terra as behavior-and-test reviewer, and Grok as security-and-tenant reviewer with explicit models.
- [ ] The behavior-and-test review checks promised behavior, scope, RED evidence, production-path persistence, integration coverage, and honest failure handling.
- [ ] The security review checks tenant scoping, permission boundaries, forged input, credential and screenshot leakage, idempotency, and data-loss paths relevant to the ticket.
- [ ] Both independent acknowledgements are required against the same final diff and evidence identity before finalization can commit.
- [ ] A finding resumes the same writer, reruns affected tests, and then reruns every applicable reviewer because the diff changed.
- [ ] Reviewer silence, timeout, or transport failure retries that reviewer without counting as a finding or silently passing.
- [ ] A sensitive visible change completes both sensitive reviews and the full visual evidence and human-review requirements selected by the planning record.
