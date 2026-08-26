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

Name a `tests.json` recipe for the story suite. Visual without a recipe has no per-slice gate.

For riftwing, use `riftwing-local-gates`. That runs `.githooks/pre-commit` (gitleaks, lockfile) with `RIFTWING_HOOK_NONINTERACTIVE=1`. It does not run pre-push fetch or Shortcut.

## Fire implement-slice

Published slug: `graphwing-implement-slice`. Manual, form, or authenticated webhook.

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

Or fire from the seat without holding the webhook key:

```bash
scripts/fire-slice.sh payload.json
```

It POSTs to this host's `/v1/rewst/fire`, which proxies to the Rewst trigger. The URL and `hook_secret` stay in `$GRAPHWING_HOME/rewst-install.json`. Needs `implement_slice_hook_url` recorded there; without it the endpoint answers 503 naming the key to set.

`kick_url` is this workflow's own webhook. After a green slice, `sliceContinue` POSTs the next ticket there. Without it, this run does one slice and stops.

On red, files stay. No `gitRestore`. Three suite-reds or a second spec-review nack parks. You continue, discard (the only wipe), split, restamp size, or tag `decision`.

Completed writer callbacks carry a `diagnostic-v1` object with a stable code, evidence
kind, and canned summary; pre-callback action/wait failures use compact diagnostic stage
receipts.
Every failover-eligible writer outcome remains
`provider_availability`, including a missing writer binary and structured provider
authentication or network evidence. `local_infrastructure` describes stack, port,
health, command, and configuration checks only; a missing non-writer executable is
`local_binary_missing`, not provider availability. The closed launcher failure taxonomy
and one-hop fallback gate are unchanged. Only compact diagnostics and provider-recovery
evidence are claimed sanitized: they never copy callback data, paths, provider output,
or traces. Other `AgentReceipt` fields retain their documented artifact values and are
not covered by that claim.

Provider recovery is a new invocation decision, never an active-session decision.
After a successful one-hop fallback, a later manual/webhook payload may set
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

`kick_url` is this workflow's own webhook. Without it the run does one fix attempt
and stops, because Rewst forbids unbounded cycles inside a run: the loop is a chain
of runs, not a cycle.

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

`auto_merge` is per-run and defaults false. When true and the PR is green, mergeable,
and carries no `hold:*` label, the run merges with `--squash --delete-branch`. The
endpoint re-reads the PR and re-decides rather than trusting the graph, and refuses
without a `run_id`, so a hand-rolled curl cannot merge anything.

## Other graphs

| Slug | When |
|---|---|
| `graphwing-verify-stack` | Stack down before e2e. Payload `{ "input": { "stack", "ports" } }`; success includes stack/port diagnostics and action failures terminate with compact diagnostic stage receipts. |
| `graphwing-pr-status` | Read-only PR checks. Unauthenticated webhook. |
| `graphwing-pr-drive` | One fix slice when CI is red. Authenticated webhook / doorbell OIDC. |

## Publish catalog graphs

Seat file `$GRAPHWING_HOME/rewst-install.json` (copy [`examples/rewst-install.example.json`](../examples/rewst-install.example.json)). Public OpenAPI URL comes from named-tunnel meta or `GRAPHWING_PUBLIC_URL`.

MCP token: `GRAPHWING_REWST_MCP_TOKEN`, or `mcp_bws_key` in that file plus `BWS_ACCESS_TOKEN`.

```bash
python3 scripts/publish_graphs.py --only all --no-run
```

Rewst rejects fan-in that is not `logic.join.*` and rejects unbounded cycles. One Graph run per slice. `kick_url` starts the next.

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
GRAPHWING_HOME=. python3 test_server.py
```
