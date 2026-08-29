# Using graphwing

Runs on the machine that has the git checkouts. Rewst org is yours (not Rewst Internal). Catalog git is [`tfournet/graphwing`](https://github.com/tfournet/graphwing).

Operator lock: [HUMAN-LOOP.md](HUMAN-LOOP.md). Do not implement in the grill pane.

## Sit down

Use your normal editor or terminal; optional Herdr session `graphwing`, tab `graph`, is a read-only job dashboard.

Allowlisted short names are `$GRAPHWING_HOME/repos.json`. Graph `cwd` / `repo` fields use those names, not paths.

## Structure a slice map

After Shortcut Ready, same tab, `/to-tickets` (from the `graphwing-loop` plugin; see [HUMAN-LOOP.md](HUMAN-LOOP.md) step 1 to install it). Graph does not Structure.

Index JSON in the app worktree (example: [`examples/slices.example.json`](../examples/slices.example.json)):

```json
{
  "tickets": [
    {
      "id": "01-login",
      "path": "slices/demo/01-login.md",
      "blocked_by": [],
      "kind": "build",
      "status": "open"
    }
  ]
}
```

Each ticket file is what the writer sees. `kind` is `build` or `decision`. `status` is `open` or `done`. Frontier is unblocked open `build` tickets. Serial: one frontier ticket at a time on the story branch.

Name a `tests.json` recipe for the story suite. Visual without a recipe has no per-slice gate, but a recipe is not browser/screenshot evidence; this catalog has no visual-proof workflow.

For riftwing, use `riftwing-local-gates`. That runs `.githooks/pre-commit` (gitleaks, lockfile) with `RIFTWING_HOOK_NONINTERACTIVE=1`. It does not run pre-push fetch or Shortcut.

## Fire implement-slice

Published slug: `graphwing-implement-slice`. Use the manual/form trigger or an authorized `/workflows/graphwing-implement-slice/run` caller with an `input` wrapper.

```json
{
  "input": {
    "repo": "riftwing",
    "branch": "story/your-branch",
    "index": "slices/demo/index.json",
    "ticket": "slices/demo/01-login.md",
    "commit_message": "feat: login tracer",
    "test": "your-named-recipe",
    "class": "mechanical",
    "size": "S",
    "kick_url": "https://engine.rewst.io/webhooks/custom/run?...",
    "e2e": "optional-named-e2e"
  }
}
```

`ticket` must already be on the frontier, or omit it and Graph takes the next build.

`ac_count` and `seams` feed `sliceRoute`, which may bump the size one step from countable features. Seven ACs took SC-110290 from floor `S` to `M`, which turned Sonnet spec-review on. Omitting them leaves the router blind and the floor unchanged, so send them honestly rather than to dodge a gate.

The legacy seat helper still exists:

```bash
scripts/fire-slice.sh payload.json
```

It POSTs to this host's `/v1/rewst/fire`, which proxies to the Rewst webhook. Do not use it as the current operator start or as rollout proof: observed webhook runs do not materialize `CTX.INPUT`, so graph inputs resolve empty. The URL and `hook_secret` stay in `$GRAPHWING_HOME/rewst-install.json`.

`kick_url` remains generic catalog wiring, but its next-run webhook uses the known-broken input mapping. It cannot continue the slice walk today; treat one manual/API run as one slice.

On red, files stay. No `gitRestore`. Three suite-reds or a second spec-review nack parks. You continue, discard (the only wipe), split, restamp size, or tag `decision`.

### Native effort contract (Phases 2–3)

Direct `POST /v1/agent/run` and `POST /v1/review/run` accept optional `effort` with the closed values `default`, `low`, `medium`, `high`, and `max`. A supplied direct-call value records `effort_source: explicit`; omission records `requested_effort: default` and `effort_source: launcher_default`. Source `route` is available only when the request also supplies the exact closed `route_execution_profile` emitted by the selected route. That `route-execution-profile-v1` object binds route version, role, work kind, class/effective size, launcher/provider/model, and requested effort; runtime reconstructs it from the versioned route catalog and rejects unknown, missing, extra, or mismatched evidence before launch. Unknown effort text returns `400 bad_effort`; a known value outside the selected native profile returns `400 unsupported_effort` before process launch.

| launcher/model | accepted request values | effective effort |
| --- | --- | --- |
| Codex `gpt-5.6-sol` (and code-off-only `gpt-5.6-terra`) | `default`, `low`, `medium`, `high`, `max` | `default` → `high`; explicit values unchanged |
| Claude `claude-opus-5`, `claude-sonnet-5` (and code-off-only `claude-fable-5`) | `default`, `low`, `medium`, `high`, `max` | unchanged; `default` emits no flag |
| Grok `grok-4.6` | `default`, `low`, `medium`, `high` | `default` → `high`; explicit values unchanged |

Installed non-inference probes established Codex CLI 0.150.1 config syntax `-c model_reasoning_effort=<value>`, Claude Code 2.1.250 session syntax `--effort <low|medium|high|xhigh|max>`, and Grok 1.0.5 ACP placement `grok agent --reasoning-effort <none|minimal|low|medium|high|xhigh|max> ... stdio`. The embedded `grok-4.6` catalog declares default `high` and advertises `low`, `medium`, `high`, and `xhigh`. Graphwing therefore maps requested Grok `default` to native `high`, exposes `low | medium | high`, omits vocabulary-external `xhigh`, and rejects parser-only/model-unproven `max`. Writer and reviewer commands share the same profile validation while preserving `workspace-write`/`read-only`, `acceptEdits`/`plan`, and ACP always-approve/read-only differences. Phase 3 maps selected route effort and matching closed evidence into implement-slice and PR-drive initial/recovered writers, fallback writers, and routed reviews. Same-session corrections copy requested effort and `route_execution_profile` from their pinned receipt identity; they cannot redraw a route or change requested/effective/source/version. These local graph fixtures do not prove tenant import, publication, or live execution.

### Closed async review contract

`POST /v1/review/run` is a closed object: `repo`, `launcher`, `provider`, `model`, and `prompt` are required; only the documented optional effort, route evidence, async, budget/turn, and webhook fields are accepted. Unknown fields return `400 unexpected_fields` before launcher lookup or spawn. A synchronous `200` receipt and asynchronous `202` job carry the same closed `review-execution-v1` identity: canonical review kind, launcher/provider/model, requested/effective/source effort, optional exact route evidence, path-free wrapper fingerprint, repo/branch/head, prompt and diff hashes, budget/turn limits, and read-only permission profile.

For a `202`, use the returned `poll` value (`GET /v1/review/jobs/{id}`) or `POST /v1/review/jobs/wait` with `{"job_id":"…","timeout_seconds":30}`. These surfaces positively accept only canonical review records; `/v1/agent/jobs/{id}` remains agent-only. Review effort provenance is separately closed: direct requests use `explicit | launcher_default`; `route` is canonical only with an exact `reviewer1 | reviewer2` route profile embedded in the sealed execution identity. Missing, writer-role, wrong-version, or mismatched profile evidence makes the request or later identity/job/receipt/runtime profile noncanonical. Acceptance serializes the canonical execution snapshot into a sealed process-local descriptor and retains the exact already-pinned wrapper descriptor in a private registry keyed by the unguessable server job ID. The worker loads that authority rather than trusting the mutable job, snapshot file, or their mutually consistent hash. Poll and wait expose only exact server-authored second-resolution UTC lifecycle timestamps; a malformed or hostile timestamp rejects the whole record and leaks no stored value. After acceptance, every ordinary `write_job` call against the existing review returns `False` under the per-job lock, including a structurally equal object or one whose keys were reordered; it does not invoke the atomic writer, replace the file, or touch persisted review bytes. The guarded queued-to-running transition authors one exact closed running record, including the server `started_at`, null `finished_at`/receipt/error/webhook values, and no unknown fields, and seals its canonical bytes and digest in private process authority. The final transition requires exact key/type/timestamp validation and canonical-byte equality with that full sealed running value. Any drift is preserved byte-for-byte, suppresses terminal success and callback, and is never repaired from stale state. Callback bookkeeping is a guarded webhook-only update against the exact committed terminal revision. Polling validates an exact closed receipt and projects allowlisted fields instead of returning stored receipt JSON. Invalid or injected records return sanitized `409` errors and leak no stored prompt, path, token, URL, provider output, or hostile timestamp.

That authority is deliberately ephemeral. If the daemon is replaced before completion, the private registry and sealed descriptors are gone and Graphwing does not reconstruct them from disk. Startup closes the review without a receipt or callback derived from stale evidence; poll/wait returns `409 review_authority_unavailable`. A fresh Rewst-owned continuation is required when that architecture exists. Completed canonical reviews remain pollable, while ordinary trusted writes cannot replace their terminal receipt. Review errors use the closed `ReviewError` shape with exactly bounded `error` and `code` strings. `POST /v1/review/run` documents a disjoint `409 oneOf`: `ReviewError` for busy/acceptance errors or `ReviewReceipt` for a synchronous execution-identity mismatch, so each real body matches exactly one branch. Launcher-fingerprint and missing-launcher `501` responses are closed `ReviewError` bodies, not receipts.

### Closed normalized usage receipt

Terminal agent and review receipts expose either `usage: null` plus one closed `usage_diagnostic`, or exactly this `normalized-usage-v1` object: `usage_version`, `fresh_input_tokens`, `cached_input_tokens`, `cache_write_tokens`, `output_tokens`, `reasoning_tokens`, nullable `provider_cost_usd`, `wall_seconds`, and nullable `turns_observed`. Counts are bounded non-negative integers; time and cost are finite non-negative numbers. Unknown provider cost is `null`, not estimated list-price dollars and not zero. Claude's reported API-dollar total is preserved when present; Codex and Grok native/subscription usage remains token-only unless their supported event contract exposes an API-dollar total.

Normalization is all-or-nothing. Missing, partial, malformed, negative, boolean, overflow, `NaN`, or infinite values return no aggregate. Public jobs, polls, waits, and callbacks contain only the numeric aggregate and closed diagnostic—never raw native events, prompt/thought/response text, command lines, logs, paths, credentials, or provider payloads. Usage is sealed with the same terminal receipt authority as status, so post-terminal drift or authority loss fails closed.

`graphwing-implement-slice` preserves those two public fields in every writer receipt object builder, including fallback and correction writers. This is workflow evidence, not durable storage. The current reviewed catalog has no Rewst data-storage action; therefore no durable outcome/economics write, idempotency key, retry/replay proof, or daemon-replacement durability is claimed. Rewst must own that write after terminal and named-test gates once a reviewed storage operation exists. Graphwing local job files, logs, caches, and registries remain replaceable execution aids.

Terminal jobs and receipts expose `requested_effort`, `effective_effort`, `effort_source`, and a path-free `launcher_version` fingerprint. Session identity binds launcher/provider/model plus all four of those profile fields and, exactly when source is `route`, the closed route profile; resume, fallback, recovery, commit, merge, and code-off evidence rejects any drift. Before launch, the worker pins one canonical execution/profile/repository snapshot. Side-effectful staging and manifest preparation happen before the final exact per-job CAS, so trusted replacements are detected and left untouched. The final critical section rejects reentrant ordinary writes; terminal agent records then reject ordinary trusted replacement while a narrow guarded API may append callback bookkeeping. A queued writer therefore cannot replace success before callback delivery, and only a successful guarded transition can authorize that callback. Missing, replaced, alien-kind, or identity-drifted jobs fail closed; unrelated worker bookkeeping may survive when persisted before preparation. Every native and code-off agent record is stamped with canonical `kind: agent`; GET, wait, callback, resume, fallback, recovery, startup recovery, code-off consumption, and writer evidence require that positive classification. Missing, malformed, unknown, or known non-agent kinds are never inferred from record shape and fail closed. At code-off launch the server writes a content-addressed complete execution manifest and anchors its hash in the enclosing immutable event chain. It binds kind/role/slot/job attribution, launcher/provider/model, every effort field and launcher version, repo/branch/starting head, prompt and budget inputs, and launch/terminal native-session attribution. Freeze and judge consumers reconstruct the job, session identity, and receipt against those immutable launch and terminal manifests; missing, legacy, unknown, or coordinated drift fails closed. Graphwing hashes one opened launcher artifact, copies it into a sealed Linux descriptor, and hands that exact immutable descriptor to process creation. The fingerprint proves the exact selected wrapper artifact bytes executed; it does not attest the interpreter, PATH-resolved mise/shim, or provider package/tool bytes subsequently resolved by that wrapper. Repository provenance accepts a path-free allowlist short name (or closed internal code-off identity), the Phase 1 safe ASCII branch subset, and a full lowercase 40- or 64-hex Git object ID. The branch contract supports current Graphwing names such as `main`, `story/your-branch`, and `feature/model-routing`; it does not claim every Git-valid ref. Unsupported punctuation and Unicode, leading `-` or slash, trailing/repeated slash, `..`, `@{`, `@`, control/space, dot-leading or dot-trailing components, and any component ending `.lock` case-insensitively are rejected. Any duplicated top-level repo, branch, or starting head must exactly match the validated session identity, and public projection uses that identity rather than the mutable duplicates. Public surfaces intentionally keep the validated `starting_head` as repository provenance, but never trust launcher/model-supplied SHA/PR URL fields as authority. Otherwise they use closed typed identifiers and canned diagnostics, excluding prompts, logs, filesystem or credential paths, callback data, secrets/tokens, raw provider output, private URLs, launcher-authored summaries, and launcher-native effort tokens. Legacy interrupted jobs without a complete mutually corroborated execution profile return a schema-valid provenance failure with nullable, explicitly untrusted profile fields. These local manifests, job files, receipts, and process-local locks are ephemeral coordination rather than durable or cryptographic authority: the lock closes races among trusted daemon writers, not arbitrary same-user filesystem mutation that ignores the protocol. Rewst data storage is the future permanent store, and the current reviewed catalog writes nothing to it.

For ordinary work, `gitCommit` requires a valid `writer_job_id`; empty/omitted IDs and arbitrary staged-index commits are rejected. Explicit add paths are allowed only with validated writer evidence or the complete existing code-off experiment/final-verification gate.

Completed writer callbacks carry a `diagnostic-v1` object with a stable code, evidence
kind, and canned summary; pre-callback action/wait failures use compact diagnostic stage
receipts.
Every failover-eligible writer outcome remains
`provider_availability`, including a missing writer binary and structured provider
authentication or network evidence. `local_infrastructure` describes stack, port,
health, command, and configuration checks only; a missing non-writer executable is
`local_binary_missing`, not provider availability. The closed launcher failure taxonomy
and one-hop fallback gate are unchanged. Compact diagnostics and provider-recovery
evidence never copy callback data, paths, provider output, or traces.

Provider recovery is a new invocation decision, never an active-session decision.
After a successful one-hop fallback, a later manual/API invocation payload may set
`recovery_version: provider-recovery-v1` and supply the prior primary/fallback routes
and receipts. The recovery endpoint re-reads both terminal job files. Missing,
malformed, mismatched, nonterminal, or out-of-order evidence stops the run. A prior
successful fallback is the only recovery state both continuation paths carry forward;
a normal or recovered-primary run omits the marker.
`missing_binary` is rechecked with the same request-time resolver used by `agentRun`
preflight and worker launch; an explicit `GRAPHWING_<LAUNCHER>_BIN` remains
authoritative. Authentication, quota, rate, network, overload, and 5xx
failures stay on fallback unless the payload also supplies a distinct, later successful
normal-primary job receipt. The check never invokes a launcher or provider. Corrections
and review-nack resumes remain pinned to the successful session receipt selected at the
start of that invocation.

Lane 66 did not run or publish a live Rewst canary. Catalog fixtures assert the
`CTX.<objectBuilder alias>.<field>` recovery-switch path and fallback precedence, using
the alias convention recorded in issue-52 git history, but they do not prove how a live
tenant version will evaluate those expressions. Publish/canary verification remains a
separate runtime step.

## Drive a PR to green

```bash
python3 scripts/drive-pr.py 3526 --message "fix: address review findings [SC-110507]"
```

It starts the run through `POST /workflows/{slug}/run` and then prints the node trace,
which is the only per-node visibility available; the run status endpoint returns nothing
but `completed`.

**Do not fire this workflow by webhook.** A webhook-triggered run never creates
`CTX.INPUT` at all, so every `{{ CTX.INPUT.* }}` in the graph resolves empty and the run
dies at `ghPrView` with no `number` on the query string. Nothing reports it: `repo` still
arrives, because `openapi.json` declares a default of `riftwing` for that parameter, so
the request looks half-correct. `fire-pr-drive.sh` posts to the webhook and cannot work.

`run_slug` wraps the body as `{"input": {...}}`. A bare payload arrives as a manual
trigger with `body: {}` and fails the same silent way.

`--message` is not optional in spirit: `git_commit` rejects an empty message, and a run
that reaches the commit has already spent a full writer session. The graph defaults it
too, so a caller that forgets loses nothing.

`kick_url` is generic catalog wiring for a fresh-run webhook, not a working attempt
loop. Under the recorded current mapping that webhook cannot create `CTX.INPUT`, so it
cannot continue `pr-drive`. One `/run` invocation is one bounded fix attempt;
`scripts/drive-pr.py` owns a multi-attempt loop by issuing fresh API runs.

The writer's prompt is not yours to write. A `findings` node reads the reviewers'
`engineering-findings-json`, dedupes by fingerprint (two reviewers raising one defect
is one fix), orders by severity, and hands over the remedies without the argument.

Two node-output traps worth knowing before editing this graph. `action.graphwing` results
are exposed as `TASKS.<node>.data.<field>`, while a `transforms.objectBuilder` publishes
its configured alias at `CTX.<alias>`. An endpoint that nests its action payload under
`data` therefore needs double `.data.data`. A large action output can instead become an
artifact stub: `TASKS.checks.data.all_green` is unreadable because that node returns
~21KB, which is why the findings endpoint runs `gh pr checks` itself and returns a small
flat result.

`auto_merge` is per-run and defaults false. Initial-green and after-fix routes both checkout the freshly read PR head and run one final named test. The earlier after-fix test remains advisory.
Only the final async job carries persisted `pr-merge-evidence-v1` repo/recipe/PR/run/head and clean start/end provenance. Its server-stamped `writer_evidence_mode` is `writer` after a fix and `initial_green` only when no writer ran; after-fix pins the successful writer/session job while initial-green has no writer reference.
For named scripts, tests, and RR recipes, supplying `response_webhook_url` forces async execution even when the catalog recipe defaults to sync, so a Graph wait always receives a terminal callback.
Merge accepts that final persisted job ID only when its repo/recipe/PR/run/head and clean start/end provenance match. It then freshly requires an open non-draft PR, a stable head, `MERGEABLE`/`CLEAN`, no holds or blocking review, and nonempty terminal-passing GitHub checks. It invokes `gh pr merge --match-head-commit <sha>` once and never retries a moved head.
Code-off receipts remain independent issue-67 commit/push gates and cannot waive this final test. `graphwing-pr-status` reports remote readiness only; `remote_ready` means `named_test_required`, not merge-safe.

## Other graphs

| Slug | When |
|---|---|
| `graphwing-verify-stack` | Stack down before e2e. Payload `{ "input": { "stack", "port" } }`; success includes stack/port diagnostics and action failures terminate with compact diagnostic stage receipts. |
| `graphwing-pr-status` | Read remote-only PR state with `{ "input": { "pr": 3624 } }` through an API start. Webhook starts remain disabled because they do not create `CTX.INPUT`. |
| `graphwing-pr-drive` | One bounded fix slice when remote checks/findings are red; start via manual/form/API run, not webhook. |

## Run a deterministic code-off

`graphwing-code-off` is one bounded DAG. Supply an unused lowercase `experiment_id`, an allowlisted repo at the exact clean `base_sha`, a 64-character lowercase hex seed, locked category/tags/source, the task, named tests, toolchain, budgets, and a commit message. Whole-value Graph interpolation may serialize tags, tests, toolchain, and budgets as bounded JSON strings; the server parses them before strict native validation. Callers never supply workspace paths; `agentRun` resolves only `{experiment_id, slot}`.

`code-off-draw-v1` uses the seed as an HMAC-SHA-256 key to rank the canonical pool and each judge's `[author-1, author-2]` order without modulo bias. The public commitment is `SHA-256("graphwing/code-off/seed-commitment/v1\\0" + seed)`; audit never returns the seed or a blind ordering before completion. There is no redraw, fallback, or cherry-picking.

The immutable record holds hash-chained events and content-addressed artifacts. Candidate/final recipes run identically with bytecode disabled in disposable Git worktrees; those worktrees are not OS isolation. Judges receive the locked task/category but no author identity. Finalization proves the frozen hash before a Git-safe target apply; ignored candidate paths, introduced identity leakage, or terminal candidate corruption park for cleanup, while infrastructure/persistence interruptions remain retryable. Audit is read-only.

`approved-model-manifest-v2` currently makes the exact Grok 4.6, GPT-5.6 Sol/Terra, and Claude Sonnet/Opus/Fable 5 identities runnable and proven for code-off. Terra and Fable remain ineligible for ordinary jobs and enter code-off only through the immutable slot identity check. The bounded catalog inventory and canary evidence was obtained 2026-08-26; this is not continuous discovery. “Latest” means approved latest at policy creation, and the at-most-30-day policy expiration parks new runs pending an explicit reviewed update. There is no dynamic provider discovery, OpenCode dependency, alias resolution, substitution, fallback, or redraw. These catalog and fixture changes do not claim that this change ran or published a live Rewst code-off.

## Publish catalog graphs

Seat file `$GRAPHWING_HOME/rewst-install.json` (copy [`examples/rewst-install.example.json`](../examples/rewst-install.example.json)). Public OpenAPI URL comes from named-tunnel meta or `GRAPHWING_PUBLIC_URL`.

MCP token: `GRAPHWING_REWST_MCP_TOKEN`, or `mcp_bws_key` in that file plus `BWS_ACCESS_TOKEN`.

```bash
python3 scripts/publish_graphs.py --only all --no-run
```

Rewst rejects fan-in that is not `logic.join.*` and rejects unbounded cycles. One Graph run handles one slice; `kick_url` is generic catalog wiring whose webhook continuation is known broken under the current input mapping.

Repository checks are fixture/catalog proof only. See [HUMAN-LOOP.md](HUMAN-LOOP.md#rollout-verification-checklist) for the separate catalog/import, published, and live-canary evidence levels; this issue does not claim a publish or canary.

After any `openapi.json` change, new operation or renamed field, re-import the integration **before** publishing graphs:

```bash
python3 scripts/reimport_integration.py       # PUT sourceUrl, then publish, records the version
python3 scripts/publish_graphs.py --only implement-slice
```

Skipping this fails silently, which is the expensive part. Rewst builds every request from its own copy of the operation list, so a field the graph sends and the server reads is dropped on the floor when the integration does not declare it. SC-110290's third run lost `response_webhook_url` that way: the review ran synchronously, its wait node blocked on a callback nobody would send, and no layer reported anything.

Two parameter names to avoid: the connector treats a query parameter called `path` as the request URL path and overwrites the endpoint with its value. `fileHead` and `gitDiff` use `rel` for that reason.

## Shortcut from the seat

Legacy seat-side tracker calls use `scripts/sc`; Graphwing model jobs do not depend on it.

```bash
sc show SC-110290            # name, type, state, epic, branches, PRs
sc states 110290             # the states available on that story's board
sc mv 110290 "In Review"     # move it
sc comment 110290 "text"     # the receipt Graph still cannot post itself
```

Every failure path exits non-zero, including an unknown state name, so a caller cannot
mistake "did nothing" for "moved it".

## Deploy to the seat

systemd runs `$GRAPHWING_HOME/server.py`, a **copy**, not a symlink. `systemctl restart`
on its own comes back green while still serving the last copy, and nothing reports that
your change is not live.

```bash
scripts/deploy.sh
```

Runs the tests, copies the code files, restarts, then re-compares to prove the live file
is the one you built. `repos.json` and `stacks.json` are tracked in git but hold
placeholders; the real ones are seat config, so deploy never touches them.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 GRAPHWING_HOME=. python3 test_server.py
```
