# Using graphwing

Runs on the machine that has the git checkouts. Rewst org is yours (not Rewst Internal). Catalog git is [`tfournet/graphwing`](https://github.com/tfournet/graphwing).

Operator lock: [HUMAN-LOOP.md](HUMAN-LOOP.md). Do not implement in the grill pane.

## Sit down

```bash
herdr --session graphwing
graphwing-idea open --label NAME --repo riftwing
```

Claude is tab `claude` in that space. Tab `graph` is the dashboard (`gw-*` logs). Do not type there.

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

`kick_url` is this workflow's own webhook. After a green slice, `sliceContinue` POSTs the next ticket there. Without it, this run does one slice and stops.

Watch tab `graph`. Do not chat there.

On red, files stay. No `gitRestore`. Three suite-reds or a second spec-review nack parks. You continue, discard (the only wipe), split, restamp size, or tag `decision`.

## Other graphs

| Slug | When |
|---|---|
| `graphwing-verify-stack` | Stack down before e2e. Payload `{ "input": { "stack", "ports" } }`. |
| `graphwing-pr-status` | Read-only PR checks. Unauthenticated webhook. |
| `graphwing-pr-drive` | One fix slice when CI is red. Authenticated webhook / doorbell OIDC. |

## Publish catalog graphs

Seat file `$GRAPHWING_HOME/rewst-install.json` (copy [`examples/rewst-install.example.json`](../examples/rewst-install.example.json)). Public OpenAPI URL comes from named-tunnel meta or `GRAPHWING_PUBLIC_URL`.

MCP token: `GRAPHWING_REWST_MCP_TOKEN`, or `mcp_bws_key` in that file plus `BWS_ACCESS_TOKEN`.

```bash
python3 scripts/publish_graphs.py --only all --no-run
```

Rewst rejects fan-in that is not `logic.join.*` and rejects unbounded cycles. One Graph run per slice. `kick_url` starts the next.

After new OpenAPI ops, re-import the custom integration from `GET /openapi.json` then publish a new integration version, then publish graphs.

## Tests

```bash
GRAPHWING_HOME=. python3 test_server.py
```
