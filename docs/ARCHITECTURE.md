# Architecture ownership

Status: active. Owner-approved in [issue #184](https://github.com/tfournet/graphwing/issues/184) on 2026-09-03.

This is the governing responsibility boundary for Graphwing. Implementation details and rollout evidence belong in the linked issues and PRs; they cannot redefine these rules.

## Product outcome

Graphwing supports one resumable, live-proven pre- and post-PR engineering lifecycle.

The pre-PR lifecycle prepares the candidate, runs declared tests and independent reviews, supports visual iteration where required, and produces a pushed PR-ready branch. The post-PR lifecycle monitors CI and review, performs bounded remediation, runs final verification, requests merge when authorized, tears down preview resources, and records the durable terminal outcome.

## Responsibility boundary

Rewst workflows own business policy and durable workflow state.

That includes:

- routing and effort selection;
- retries, continuation, handoff, restructure, and park decisions;
- review actionability and writer-brief construction;
- lifecycle transitions and durable attempt history;
- code-off participant policy, stages, judgments, winner selection, and economics;
- merge eligibility and merge intent;
- preview teardown and terminal outcomes.

Graphwing exposes normalized facts and performs bounded local effects.

The daemon owns only:

- bounded local execution;
- normalized facts from git, GitHub, tests, launchers, and providers;
- authentication and allowlists;
- launcher pinning and permission profiles;
- immutable identity and receipt verification;
- atomic local effects, including exact git operations;
- private code-off seeds, blinding material, cryptographic hashing, isolated worktrees, and exact tree promotion;
- non-bypassable safety rejection.

A workflow cannot weaken daemon safety. The daemon may reject an unauthorized, stale, malformed, unsupported, or unsafe request, but it does not select a business route or next lifecycle state.

## Merge

The workflow decides whether merge is eligible and requested. Graphwing executes an exact merge only after freshly revalidating authorization, PR head, final named-test evidence, checks, holds, and merge state. Human authorization remains the source of permission for automatic merge.

## Durable state

Rewst data storage is the only permanent owner of workflow state, attempt history, judgments, economics, and terminal outcomes. Graphwing job files, manifests, caches, sessions, locks, ledgers, and local databases are disposable execution authority or corroboration. Losing them may park active work; it cannot erase or recreate a completed durable outcome.

## Workflow implementation

Business transformations use deterministic native Rewst nodes. Code Expression and procedural Nunjucks are last resorts and require evidence that native nodes cannot express the operation.

Migrations are incremental. Routing, PR lifecycle, run control, and code-off may move independently where their contracts do not overlap. Each replacement must be imported, published, read back, and live-proven before the superseded daemon policy is removed. Final daemon convergence follows those migrations; there is no big-bang rewrite or arbitrary line-count target.

## Operational authorization

Source implementation and high-confidence Graphwing PR merges are authorized.

Live deployment, OpenAPI re-import, workflow publication, live canaries, and destructive cleanup require separate direct approval.

Destructive cleanup includes deleting preserved historical branches, forensic worktrees, or runtime evidence. Ordinary removal of a newly merged task worktree remains part of normal PR completion.

## Program

- [#185](https://github.com/tfournet/graphwing/issues/185): PR correction and merge policy
- [#186](https://github.com/tfournet/graphwing/issues/186): model routing and recovery policy
- [#187](https://github.com/tfournet/graphwing/issues/187): run-control lifecycle policy
- [#188](https://github.com/tfournet/graphwing/issues/188): code-off experiment policy
- [#189](https://github.com/tfournet/graphwing/issues/189): minimal execution-adapter surface and final convergence
- [#52](https://github.com/tfournet/graphwing/issues/52): pre- and post-PR product outcome

Active-path regressions remain independently fixable and must not wait for this migration.
