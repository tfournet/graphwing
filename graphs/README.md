# Published graphs

Hard-coded topology. Payload is the iteration. New file only when **edges** change.
Rewst publish requires fan-in on `logic.join.*` and no unbounded cycles (one Graph run per slice; `kick_url` starts the next).

How a human gets to a run: `docs/HUMAN-LOOP.md` and `docs/USING.md`. Slice map is `index` JSON in the worktree. `implement-slice` walks it serially. `pr-drive` keeps files on red.

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
| `graphwing-visual-evidence` | `build_id`, `event_id`, optional `reviewer` | strict preflight → stable preview → real browser before/candidate → inspect/one recapture → automated UI review → durable artifact handoff → Rewst GitHub issue files/comment → optional exact planning-pane prompt; terminal pre-commit with preview alive |
| `graphwing-pr-drive` | Manual, form, or authenticated webhook: `repo`, `pr`, `test`, `prompt`, `commit_message` | mergeable = all_green and not blocking reviews; red → checkout PR head → agent → testRun → commit/push or keep files on red → checks again |
| `graphwing-pr-lifecycle` | `build_id`, `event_id`, `event`, `pr`, optional `choice` | one authenticated lifecycle receipt; no loop |
| `graphwing-pr-status` | Manual or unauthenticated webhook: `pr` | ghPrView → ghPrChecks → mergeable, red, pending, or blocking reviews; no writes |
| `graphwing-build-completion-supervisor` | No input: `trigger.workflowCompleted` on every terminal live status of workflows tagged `graphwing-supervised` | supervisorReconcile → switch on `action` → receipt, watch, relaunch, retry_event, park, circuit_open, ignored; parks read supervisorReport |

The supervisor listener is the one graph with no manual trigger and no input.
Its typed `completion_payload` objectBuilder preserves `sourceWorkflow.tags` as
an array. The custom-action boundary cannot preserve a collection directly, so
that one value is compact JSON and `supervisorReconcile` strictly decodes only
a bounded string array; the public OpenAPI contract remains array-first.
It cannot select itself (`excludeSelf`, plus a server-side `self_listener`
refusal), it never kicks the failed run back and never posts to an expiring
`action.wait.webhook` callback, and it has no human wait node: a park is a
durable report an operator fetches with `supervisorReport`, not a run held open
waiting for one. The launcher must call `supervisorRegister` before the source
run starts, because a run that fails before declaring any output still has to
be reconcilable.

Retry class is derived only from trusted terminal status. Circuit identity is
registered workflow ID/version plus bounded node/code when the authenticated
listener has authoritative trigger or declared metadata. The completion event
does not promise raw failed-node trace detail, so missing detail uses canonical
`unknown` plus terminal status/error presence. Distinct available details stay
separate until the bounded circuit map uses its deterministic overflow key.
