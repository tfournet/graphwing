# Published graphs

Hard-coded topology. Payload is the iteration. New file only when **edges** change.

`$GRAPHWING_INSTANCE` and the webhook secret placeholder are replaced at publish from environment/install configuration.
Public OpenAPI URL comes from named-tunnel meta (`cloudflared-meta.json`) or `GRAPHWING_PUBLIC_URL`.
riftwing rings graphwing OIDC, no Rewst secrets.

```bash
GRAPHWING_REWST_MCP_TOKEN=... python3 scripts/publish_graphs.py --only pr-drive
```

Copy `examples/rewst-install.example.json` to `$GRAPHWING_HOME/rewst-install.json` and fill org/instance IDs. Do not commit the filled file.

| Slug | Input | Shape |
|---|---|---|
| `graphwing-verify-stack` | `stack`, `ports` | stackStatus → portCheck |
| `graphwing-implement-slice` | Manual, form, or authenticated webhook: `repo`, `branch`, `prompt`, `commit_message`, `test`, optional `iters_left=2` | gitStatus → gitCheckout → wait-webhook agentRun → testRun → commit/push; on failed test restore, `iters_left=2` runs one unrolled retry, otherwise waits for human acknowledgement |
| `graphwing-pr-drive` | Manual, form, or authenticated webhook: `repo`, `pr`, `test`, `prompt`, `commit_message` | mergeable = all_green and not blocking reviews; red → checkout PR head → agent → testRun → commit/push or restore → checks again; webhook doorbell on CI completed + review submitted |
