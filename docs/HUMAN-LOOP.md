# Human loop (operator lock)

How Tim turns an idea into a PR. Graph owns topology. This pane (Claude) interviews. graphwing is named ops.

Read this at the start of a **new** Herdr/Claude session. Do not reconstruct it from chat history.

## Surfaces

- Session: `herdr --session graphwing` (only session).
- Dashboard: space `graphwing`, tab `graph`. Job logs `gw-*`. Do not chat there.
- One idea = one **space** (`graphwing-idea open --label NAME --repo riftwing`). Claude is tab `claude`.
- Pre-platform notes: Obsidian `Epics/`, `Notes/`, `projects/riftwing/`. **Not** Mission Control.
- Tracker: Shortcut. In-repo truth after cement: ADR / `ARCHITECTURE.md` / story.

## Flow

1. **Grill** — tab `claude`, `/grill-with-docs`. Skills: `rewst-obsidian-staging`, `rewst-graph-handoff` (`~/.claude/skills/`). Matt plugin `mattpocock-skills`. Do not `/implement` or `/build` here.
2. **UI** — Storybook throwaways; Tim picks. Then agents may build.
3. **Spec** — `/to-spec` onto Shortcut. Cemented spec **is** the start signal.
4. **Write** — Rewst Graph `graphwing-implement-slice` (`repo`, `branch`, `prompt`=spec, `commit_message`, `test`, `iters_left`). Watch tab `graph`.
5. **Proof (Tim)** — bug: the failing command is green. UI: drive a live stack. Minor UI: before/after on the story.
6. **PR** — push, then GitHub CI (deterministic) then Engineering grade (AI). Merge is Tim. Doorbell can start one fix run if CI is red.

## Models (ID + launcher, not product nicknames)

| Job | Model ID | Launcher |
|---|---|---|
| Grill / spec / design | `claude` CLI default; `claude-opus-5` if auth/RLS/tenancy | `claude -p`, `HOME=/home/tim`. Never Hermes Anthropic OAuth. |
| Write, default | `grok-4.6` | Graph `agentRun`, Hermes `~/.graphwing`, `provider: xai-oauth`. **Not wired** — seat default is still `gpt-5.6-sol` / `openai-codex`. |
| Write, visual | `claude-opus-5` | Graph worker = `claude -p`, not Hermes. **Not wired.** |
| Spec-review after tests | `claude-sonnet-4-5`; Opus if auth/schema/tenant | `claude -p --permission-mode plan`, one shot. |
| Evidence | `grok-4.6` | Delegate/fallback. Does not commit. |

Grill sets a **class** (`mechanical` / `visual` / `sensitive`). Graph looks up model+launcher. Do not say “use Codex” — that is a wire (`openai-codex`), not a policy.

## Tests fail (pre-PR, today)

Codex/Hermes wrote files; Graph ran `test`. Red → `git restore` + `git clean -fd` (uncommitted work deleted). Optional one more write from the spec (`iters_left=2`), not from the failure log. Second red → wipe again → wait for Tim on an empty tree.

**Intended:** keep the files, feed the test log into the next write, do not commit red. Wipe only on explicit discard. **Not wired.**

## Not wired yet (do these next, do not re-litigate)

1. Spec on Shortcut Ready → `POST graphwing-implement-slice` (no bash `go`).
2. Keep files on red; fix-loop with the test log.
3. Seat default writer `grok-4.6`; class → model table on `agentRun`.
4. Superpowers is already **disabled**. Leave it. Anthropic `code-review` plugin clashes with Matt `/code-review` if both enabled.

## Do not

- Mission Control in the grill.
- A Rewst App as the inbox.
- Second Herdr session or `~/.hermes/profiles/*` as the human seat.
- Implement in the grill pane.
- Grok as judge (grill/merge/spec-review).
