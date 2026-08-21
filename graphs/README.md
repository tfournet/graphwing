# Published graphs

Hard-coded topology. Payload is the iteration. New file only when **edges** change.

How a human gets to a run: `docs/HUMAN-LOOP.md`. Slice map is `index` JSON in the worktree. `implement-slice` walks it serially. `pr-drive` keeps files on red.

`$GRAPHWING_INSTANCE`, `$GRAPHWING_STATUS_REPO`, and the webhook secret placeholder are replaced at publish from environment/install configuration.
Public OpenAPI URL comes from named-tunnel meta (`cloudflared-meta.json`) or `GRAPHWING_PUBLIC_URL`.
riftwing rings graphwing OIDC, no Rewst secrets.

The GitHub Action reads `<!-- pr-drive-hook: URL -->` from the PR body and POSTs `{pr}` to that Rewst webhook, so no GitHub secret is required. The status graph webhook has no hook secret; the fix graph (`pr-drive`) remains authenticated with `x-rewst-secret`.

```bash
GRAPHWING_REWST_MCP_TOKEN=... python3 scripts/publish_graphs.py --only pr-drive
```

Copy `examples/rewst-install.example.json` to `$GRAPHWING_HOME/rewst-install.json` and fill org/instance IDs. Do not commit the filled file.

| Slug | Input | Shape |
|---|---|---|
| `graphwing-verify-stack` | `stack`, `ports` | stackStatus → portCheck |
| `graphwing-implement-slice` | `repo`, `branch`, `index`, optional `ticket`, `commit_message`, `test`, `class`, `size`, optional `kick_url` | frontier → route → fileHead → agentRun → test → opposing spec-review → complete → commit → push → continue |
| `graphwing-pr-drive` | Manual, form, or authenticated webhook: `repo`, `pr`, `test`, `prompt`, `commit_message` | mergeable = all_green and not blocking reviews; red → checkout PR head → agent → testRun → commit/push or keep files on red → checks again |
| `graphwing-pr-status` | Manual or unauthenticated webhook: `pr` | ghPrView → ghPrChecks → mergeable, red, pending, or blocking reviews; no writes |
