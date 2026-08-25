# Human loop (operator lock)

How an engineer turns an idea into a PR. Graph owns topology. This pane (Claude) interviews. graphwing is named ops.

Read this at the start of a **new** Herdr/Claude session. Do not reconstruct it from chat history.

**Human** = any engineer on their machine (not Rewst Internal).

Two different "graphs". Do not smash them:

| | Rewst workflow (topology) | Slice map (our files) |
|---|---|---|
| What | `implement-slice` (+ thin walker): how **one** slice is done | Tickets + `blocked-by` in the worktree: **which** slice |
| When it changes | Rare. New JSON only when **edges** change | Every idea. Structure writes it after Ready |
| Graph input | Same published workflow | `repo` + ticket **path**. Loop iterations = `input` |

## Surfaces

- Session: `herdr --session graphwing` (only session).
- Dashboard: space `graphwing`, tab `graph`. Job logs `gw-*`. Do not chat there.
- One idea = one **space** (`graphwing-idea open --label NAME --repo riftwing`). Claude is tab `claude`.
- Pre-platform notes: Obsidian `Epics/`, `Notes/`, `projects/riftwing/`. Mission Control is **retired** (archive: `/home/tim/archive/riftwing-mission-control-20260821`). Do not recreate it.
- **Shortcut** = inbound **idea** (external handle). Not the implementation queue.
- **Progress** = markdown slice map in the app worktree. Do not mirror ACs onto the Shortcut card.

## Flow

1. **Grill.** Tab `claude`, `/grill-with-docs`. Steps 1, 3 and 5 ship in the `graphwing-loop` plugin (`plugins/graphwing-loop/`); `install.py` offers it, or `claude plugin marketplace add <checkout> && claude plugin install graphwing-loop@graphwing`. A same-named skill in `~/.claude/skills/` **wins over the plugin**, so remove any leftover `grilling` / `to-spec` / `to-tickets` there. Seat skills: `rewst-obsidian-staging`, `rewst-graph-handoff` (`~/.claude/skills/`). Matt plugin `mattpocock-skills` supplies `/grill-with-docs` and `domain-modeling`. Do not `/implement` or `/build` here. Grill stamps a **class** (`mechanical` / `visual` / `sensitive`) and a story size **floor** (`S` / `M` / `L`); both ride in `index.json`, never on the tracker card.
2. **UI.** Storybook throwaways; the engineer picks. Then agents may build.
3. **Spec.** `/to-spec` onto Shortcut. The Shortcut story is the external idea, not the slice list.
4. **Ready** (Shortcut). Spec is good enough to **Structure**. Graph does **not** write yet.
5. **Structure.** Same tab `claude`, `/to-tickets`. Writes the slice map in the worktree. The engineer approves once. Graph never Structures.
6. **Write.** A **frontier** ticket path fires `graphwing-implement-slice`. Graph **walks serially** until the map is empty, a slice **fails** the valve, or the next ticket is a **`decision`**. Watch tab `graph`; do not chat there.
7. **E2E.** After the map is empty, **before** PR/CI: named smoke/e2e on the seat. Green is supposed to comment `slices complete` (Graph does not post it yet). Red goes back to slices (below).
8. **Proof.** Bug: the failing command is green. UI: drive a live stack. Minor UI: before/after on the story. You, not Graph.
9. **PR.** Then GitHub CI (deterministic) then Engineering grade (AI, same vendor table as spec-review). Graph does not move Shortcut columns; you close the story after proof + merge.
10. **Drive to green.** `python3 scripts/drive-pr.py <pr> --message "..."`. It reads the reviewers' `engineering-findings-json` itself, so nobody writes a fix brief. Green-but-graded counts as red: a PR that passes CI and still carries findings goes to the fix path, not to mergeable. One fix slice → test → commit → push → re-read findings → kick the next attempt, up to `max_attempts` (default 3). **Not by webhook**: a webhook run never creates `CTX.INPUT` and dies at the first parameterised node while Rewst reports it completed. See [USING.md](USING.md#drive-a-pr-to-green).
    - Read `/runs/{id}/trace` when a run does nothing. The status endpoint only ever says `completed`, and the HTTP access log cannot tell you which branch a switch took.
    - Gates must never read `gh pr checks` output: it is ~21KB and Rewst swaps an output that large for an artifact stub, which a filter passes without evaluating.
11. **Merge.** Yours by default. A run started with `auto_merge: true` may merge when it is green, mergeable, and unheld; the endpoint re-reads the PR and re-decides rather than trusting the graph. Merge requires a `run_id`, so it is reachable only from inside a run.

## Slice map

Structure cuts on **behavior**, not budget (not “fill a context window”):

- Demoable vertical tracer bullet (not horizontal layers).
- 3–8 testable ACs (Bob’s contract; **not** Gherkin-as-a-language).
- One primary seam. Title with “and”, or two journeys → split.
- `blocked-by` edges. No cycles. **Frontier** = unblocked open **build** tickets.
- Checker may **nag** if `>8` ACs, `>1` primary seam, or it will not fit **L**. The engineer still cuts.
- `S`/`M`/`L` is **not** why you split. Size is stamped later (below).

Writer sees **only** that ticket file. Not the grill, not the parent spec.

A ticket tagged **`decision`** (grill/prototype) is not a build. Graph **parks** (idea space + `gw-*` log). The writer may **promote** fog to a `decision` ticket and stop; it must not guess. After you grill, Structure may add build tickets; the walker resumes.

**Serial.** Two frontier tickets do not run at once on the same branch. `AGENT_MAX_CONCURRENT` is a throttle, not fan-out.

## One slice (`implement-slice`)

Payload is the **ticket path**, not the whole spec. Same workflow JSON; next slice = next `input`. `implement-slice` `fileHead`s that path after checkout; `agentRun.prompt` is the ticket text.

1. `gitCheckout` the story branch (create if needed).
2. One **Hermes session per slice** (`agentRun`). Inner TDD is this session.
3. Graph `testRun` (allowlisted recipe): this ticket’s tests **plus** the story suite. Visual still needs a named recipe (smoke / tiny happy path counts). E2E is **not** this gate.
4. **Spec-review** (opposing vendor, plan mode) **gates commit**. Mechanical **S skips**. Nack = keep files, no sha, no Shortcut comment.
5. Then `gitCommit` + `gitPush`. Writer **stages**; Graph commits. No sha until green (+ review unless S).
6. Lossy Shortcut comment is the intended sha receipt. Graph does not post it yet.

`gitCommit` does not `git add -A`. Empty commit = failed slice (keep files, do not push).

## Class × size (deterministic)

Closed table. Graph looks up the row. No per-slice model picker. Missing class → `mechanical`. Story size floor is **yours**; Graph may bump **one** step from **countable** features (AC count, seams, class) at the **start** of the ticket. Never down. Never across class. **Never because tests are red.**

**What the classes mean.** They pick the writer, so they are about what the code
is, not how hard it feels:

- **`sensitive`** — auth, permissions, tenant isolation, RLS, credentials, or
  anything that can lose or leak data. Two independent acks before commit.
- **`visual`** — anything a person looks at, **and all TypeScript
  implementation**. Frontend, components, hooks, and the TS that feeds them are
  Opus work: this is where design judgement and type-level reasoning live, and
  it is not what the mechanical writer is for. `visual` still needs a named test
  recipe; no recipe, no visual slice.
- **`mechanical`** — everything else. Scripts, config, infra, Go, test harnesses.
  No visible surface, no TypeScript, no security boundary.

Stamp the highest class that applies. A TypeScript change to an auth boundary is
`sensitive`, not `visual`.

| class | writer launcher / model | spec-review (plan mode) |
|---|---|---|
| `mechanical` | Hermes `agentRun` `grok-4.6` (`xai-oauth`) | Anthropic Sonnet |
| `visual` | `claude -p` `claude-opus-5` (not Hermes) | **Terra** (`gpt-5.6-terra`, OpenAI) |
| `sensitive` | `claude -p` `claude-opus-5` | **Terra, then Grok** (both must ack) |

**The reviewer's vendor is never the writer's, and never the planner's.** Two
separate constraints, and they are what fix the table:

| seat | profile | vendor |
|---|---|---|
| planner | `riftwing-planner` `gpt-5.6-sol` | OpenAI |
| writer (visual/sensitive) | `riftwing-autopilot` / `riftwing-oneshot` `claude-opus-5` | Anthropic |
| investigator | `riftwing-investigator` `grok-4.6` | xAI |

All three vendors review, and no vendor ever reviews itself:

- `mechanical`: xAI writes, **Anthropic** grades it.
- `visual`: Anthropic writes, **OpenAI** grades it.
- `sensitive`: Anthropic writes, **OpenAI and xAI** both grade it.

For `visual` and `sensitive` the writer is Anthropic, so no Anthropic model may
review — that rules out Sonnet, Opus **and Fable**. Sol is excluded separately,
as the planning session, so OpenAI is represented by Terra: same vendor,
different model, so the spec's author is never in the review chain.

Reviewers running through Hermes are always launched with an explicit `-m`.
Without it every one of them silently takes the seat's default profile, and
"Grok reviewed it" would only ever mean "whatever `config.yaml` said today".

Example budgets (tune later; Graph wait ≥ budget):

| size | mechanical turns / wait |
|---|---|
| S | 10 / 120s |
| M | 30 / 300s |
| L | 50 / 600s |

Visual/sensitive use the same size letters with longer waits. **Timeout with no test verdict** may bump size **once** (same class/model); that bump **is** the new stamp. Red tests do **not** bump size or model.

Engineering grade on the PR uses this **same** mapping on the whole diff (majority writer). Tie → Sonnet.

The **writer’s vendor does not spec-review**. Grok does not grill or merge. Evidence may be Grok; it does not commit.

Do not say "use Codex". That is a wire (`openai-codex`), not a policy.

## Retry (one session per slice)

Resume the **same** Hermes session. Compact signal only (failing names + short tail). Keep files. Reviewer chain-of-thought never enters the writer session.

| Event | Session | Then |
|---|---|---|
| Inner TDD red | keep | compact output; continue |
| Outer `testRun` red | **resume** | compact suite fail |
| Spec-review nack | **resume** if turns remain | compact must-fix list |
| Timeout, no verdict | new window, one size bump | new stamp |
| Stamp gone, or **3** suite-reds, or **3** finished opposing-review **chains** that still nack | **park** | idea space + tab `graph` log. Shortcut silent |

After park: you **continue** (same ticket), **discard** (wipe, explicit), **split**, restamp size, or tag `decision`. Graph does not open a blank chat to dodge the gate. Discard is the only wipe.

Strike = a finished review **chain** (sensitive = Sol then Opus) that still says no, not every nit in one plan-mode job. After a nack retry goes green, the **whole** review chain reruns. No sha until it acks.

## E2E (after the map, before CI)

Not per slice. After frontier empty:

1. Stack down → `verify-stack` / park infra. Not a product slice.
2. Named e2e/smoke from `tests.json`.
3. **Green** → intended Shortcut comment `slices complete` (not posted yet). Then proof, then PR, then CI.
4. **Red** → no `slices complete`, no PR. Compact e2e signal.
   - If failing names are already ACs: Graph **auto-adds** a build ticket and walks it (new session, writer sees only that ticket + compact fail). Then e2e **again**.
   - If the output is a blob: Graph **drafts** the ticket; you ACK; then it walks.
5. **Three e2e-reds** on the story → park. Then you add tickets (the only time the default is you-as-queue).

Do not resume the last feature session to “fix e2e.”

## Tests fail (pre-PR)

`graphwing-implement-slice` **keeps files** on red. No `gitRestore`. No commit. Two compact resumes of the same Hermes session, then park (three suite-reds). Spec-review nack resumes once with compact review, then park if it nacks again.

`graphwing-pr-drive` keeps files on red (no restore).

## Known gaps (do not re-litigate here)

Future work lives in Obsidian `Riftwing/Notes/Graphwing human loop.md`. This list is what an engineer hits today.

1. Structure is human `/to-tickets`. Graph never cuts tickets.
2. Spec-review nack resumes **once** (compact must-fix, same Hermes session), then parks. Wipe only on explicit discard.
3. Graph does not post Shortcut comments (ticket + sha, or `slices complete`).
4. Superpowers is already **disabled**. Leave it. Anthropic `code-review` plugin clashes with Matt `/code-review` if both enabled.
5. **Re-import the integration after any `openapi.json` change**: `python3 scripts/reimport_integration.py`, then publish graphs. Rewst builds every request from its own copy of the operation list, so a field the graph sends and the server reads is dropped silently if the integration does not declare it. No error at any layer.

The mechanical writer is `grok-4.6` via Hermes, confirmed on the first real run (SC-110290, 30 turns, 300s). The graph passes `model` explicitly from `sliceRoute`, so the seat's `config.yaml` default does not apply to slices and does not need flipping.

`POST /v1/slice/e2e` runs after the map is empty when `e2e` is set: green/skip complete, `FAIL:` lines auto-add a ticket, blob drafts, three reds park. Next slice is `kick_url`, not a cycle inside one run.

How to fire a run: [USING.md](USING.md).

## Do not

- Mission Control in the grill.
- A Rewst App as the inbox.
- Second Herdr session or `~/.hermes/profiles/*` as the human seat.
- Implement in the grill pane.
- Compile the slice map into Rewst nodes.
- Mirror the slice DAG onto Shortcut workflow columns.
- Let the writer’s vendor spec-review its own slice.
- Escalate model or L because tests are red.
- Run full e2e on every slice.
- Parallel writers on the same story branch.
