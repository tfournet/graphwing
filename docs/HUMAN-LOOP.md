# Human loop (operator lock)

How an engineer turns a GitHub issue into a PR. Rewst Graph owns topology. Graphwing exposes bounded local operations.

Use your normal editor or terminal. Do not reconstruct operating rules from an old chat transcript.

## Surfaces

- GitHub Issues hold the approved idea and acceptance criteria. The implementation queue is the slice map in the worktree.
- Pre-platform notes live outside this repo.
- Native `codex`, `claude`, and `grok` launchers must already be installed. Graphwing never installs or wraps them.
- Herdr is optional. Session `graphwing`, tab `graph`, is a deterministic job dashboard. Do not use it as a planner or chat seat.

## Flow

1. Grill the idea and record the accepted behavior on its GitHub issue.
2. Stamp class (`mechanical`, `visual`, or `sensitive`), work kind (`go_coding`, `typescript_coding`, or `research_ops`), and size floor (`S`, `M`, or `L`).
3. Structure the approved work into a slice map in the app worktree. Graph does not invent slices.
4. Fire `graphwing-implement-slice` with one frontier ticket path.
5. Run named local tests from `tests.json`. On red, files stay on disk and the run parks.
6. After the map is empty, run the named smoke or e2e recipe before opening a PR.
7. Open the PR, verify CI and independent review, then merge.

## Slice map

- Cut on behavior, not token budget.
- Prefer a demoable vertical slice with 3-8 testable acceptance criteria and one primary seam.
- `blocked_by` edges form a DAG. Frontier means an unblocked open `build` ticket.
- A `decision` ticket parks the workflow until the engineer resolves it.
- Run one writer at a time on a branch.
- The writer sees the ticket file, not the planning transcript.

## One slice

1. `gitCheckout` selects or creates the branch.
2. `sliceRoute` chooses an explicit native launcher, provider, and model from work kind.
3. `agentRun` starts one bounded native session and returns a receipt with structured session identity.
4. `testRun` runs the allowlisted ticket and story recipe.
5. Opposing-provider review gates commit when required.
6. The writer stages named files. Graph commits and pushes only after green gates.

Go uses Codex, TypeScript uses Claude, and research/operations uses Grok. Mechanical S skips model review. Other work gets one opposing-provider review; sensitive work gets two. Missing launchers, model mismatches, untraceable resumes, missing session identity, and invalid receipts fail closed.

`gitCommit` does not run `git add -A`. An empty commit is a failed slice. Keep files and do not push.

## Retry and park

Resume only the native session identity recorded in a successful Graphwing receipt. Send compact failing test names or must-fix review items, not reviewer reasoning. Never switch launchers implicitly.

A timeout without a verdict, exhausted retry budget, repeated red suite, or repeated review rejection parks the slice with files intact. Discard is the only operation allowed to wipe work.

## Do not

- Install or manage model launchers through Graphwing.
- Add a planner or chat seat to Herdr.
- Implement in the grill session.
- Let a writer's provider review its own work.
- Escalate model or size because tests are red.
- Run full e2e on every slice.
- Parallelize writers on one story branch.

How to fire a run: [USING.md](USING.md).
