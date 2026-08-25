# 03-visual-evidence: Publish one visual evidence round

**Behavior:** A visible change produces trustworthy live-browser evidence in Shortcut and, when requested, a review-ready message in the planning session before any commit.

**Seam:** The visual-evidence round transition.

**Context:** Visual proof is independent from routing class. A sensitive UI change still uses sensitive model routing and also requires visual proof. The planning record names the real route, setup and navigation steps, expected state, viewports, proof checklist, and an explicit yes or no for human visual review. The workflow keeps a preview stack running and drives the actual served Rewst page. Synthetic servers, DOM-only probes, and generated summary graphics do not count as primary evidence. Every visual round posts real screenshots to the Shortcut story even when human review is disabled. Screenshot bytes cross through a short-lived Graphwing artifact handoff; Rewst uses its Shortcut integration to upload and comment, so the laptop never receives a Shortcut credential.

## Acceptance criteria

- [ ] Visual preflight requires a complete proof contract and an explicit human-review yes or no; there is no default answer.
- [ ] The preview stack starts or resumes with stable identity and reports the exact reachable URL plus any login and navigation instructions.
- [ ] The browser executes the declared scenario on the real served page and records the expected visible state, accessibility result, console result, and relevant request failures.
- [ ] An automated UI/accessibility reviewer receives the ticket, final diff for the round, browser observations, and screenshots, then returns focused findings or an acknowledgement.
- [ ] Before upload, image inspection verifies legibility, required labels and values, unobscured target UI, correct route, and correct visible tenant or project context; failed images are recaptured.
- [ ] The first round uploads before and candidate images; each later round uploads a new candidate set and identifies the prior round it supersedes without deleting history.
- [ ] Rewst uploads the images as files associated with the Shortcut story and posts a comment naming file IDs, route, scenario, visible proof, automated checks, review status, and round number.
- [ ] When human review is enabled, Herdr prompts the saved planning agent with the live URL, viewing instructions, checklist, screenshots, and available actions; the workflow remains pre-commit and leaves the preview stack running.
