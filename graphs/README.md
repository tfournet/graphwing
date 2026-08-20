# Published graphs (org tim-graphwing)

Hard-coded topology. Payload is the iteration. New file only when **edges** change.

`$GRAPHWING_INSTANCE` is replaced at publish from `rewst-install.json`.

```bash
python3 scripts/publish_graphs.py --only pr-drive
```

| Slug | Input | Shape |
|---|---|---|
| `graphwing-verify-stack` | `stack`, `ports` | stackStatus → portCheck |
| `graphwing-implement-slice` | `repo`, `branch`, `prompt`, `commit_message` | gitStatus → gitCheckout → wait-webhook agentRun → gitCommit → gitPush |
| `graphwing-pr-drive` | `repo`, `pr`, `test`, `prompt` | checks green → done; red → testRun → one agent slice → checks again |
