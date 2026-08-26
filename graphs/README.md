# Published graphs

Hard-coded topology. Payload is the iteration. New file only when **edges** change.
Rewst publish requires fan-in on `logic.join.*` and no unbounded cycles. One Graph run remains bounded; the cataloged `kick_url` edge targets a fresh webhook run, but the current webhook-to-`CTX.INPUT` path is known broken and unusable.

How a human gets to a run: `docs/HUMAN-LOOP.md` and `docs/USING.md`. Slice map is `index` JSON in the worktree. `implement-slice` walks it serially. `pr-drive` keeps files on red.

`$GRAPHWING_INSTANCE`, `$GRAPHWING_STATUS_REPO`, and the webhook secret placeholder are replaced at publish from environment/install configuration.
Public OpenAPI URL comes from named-tunnel meta (`cloudflared-meta.json`) or `GRAPHWING_PUBLIC_URL`.
riftwing rings graphwing OIDC, no Rewst secrets.

The catalog declares an unauthenticated `pr-status` webhook and authenticated `implement-slice` / `pr-drive` webhooks, but current webhook runs do not create `CTX.INPUT`. The `pr-status` webhook is known non-operational, as are webhook starts and chained continuations for the other two graphs; use a manual/form or `/workflows/{slug}/run` start with `{ "input": {...} }`.

`.github/workflows/pr-drive-doorbell.yml` still sends `pr` to the authenticated `pr-drive` webhook. It relies on that broken input mapping and is not rollout proof or a current operator start; do not infer readiness from the workflow's presence.

```bash
GRAPHWING_REWST_MCP_TOKEN=... python3 scripts/publish_graphs.py --only pr-drive
```

Copy `examples/rewst-install.example.json` to `$GRAPHWING_HOME/rewst-install.json` and fill org/instance IDs. Do not commit the filled file.

| Slug | Input | Shape |
|---|---|---|
| `graphwing-verify-stack` | `stack`, `ports` | stackStatus → portCheck; success retains `diagnostic-v1`, while action failures end in compact diagnostic stage receipts |
| `graphwing-implement-slice` | `repo`, `branch`, `index`, `ticket`, `commit_message`, `test`, `iters_left`, `class`, `work_kind`, `size`, `ac_count`, `seams`, `kick_url`, `kick_token`, `e2e`, `recovery_version`, `prior_primary_route`, `prior_primary_receipt`, `prior_fallback_route`, `prior_fallback_receipt`, `fresh_primary_receipt` | frontier → route → fileHead → optional later-invocation `provider-recovery-v1` evidence recheck → one-hop writer/fallback → named test → opposing review → complete → commit → push; active corrections stay pinned, red parks with files, and recovery/catalog coverage is fixture-only |
| `graphwing-pr-drive` | `repo`, `pr_number`, `attempt`, `max_attempts`, `auto_merge`, `kick_url`, `kick_token`, `test`, `class`, `size`, `work_kind`, `prompt`, `commit_message` | remote state → at most one writer fix + advisory test → commit/push → fresh head checkout → one persisted final named test → optional strict evidence-ID/match-head merge; all refusal paths terminate safely |
| `graphwing-pr-status` | `pr` | remote-only fresh PR/check classification: no-checks, red, pending, blocking review, malformed/unknown/inconsistent, or `remote_ready` with `named_test_required`; no named-test job or git write |
| `graphwing-code-off` | `experiment_id`, `repo`, `base_sha`, `seed`, `category`, `tags`, `category_source`, `prompt`, `tests`, `toolchain`, `budgets`, `commit_message` | immutable deterministic prepare → two frozen/tested candidates → three blind judged receipts → aggregate → final test/hash proof → commit/push gate; production currently parks before launch because Fable/Terra are unproven; worktrees are not OS isolation; no redraw, fallback, cycle, or merge |

These are repository catalog claims, not Rewst import, publish, or live-canary receipts. There is no visual-proof graph in this catalog.
