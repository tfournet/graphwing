---
name: to-tickets
description: Break a spec or the current conversation into a graphwing slice map. Writes an index JSON plus one self-contained ticket file per slice into the app worktree, for Graph to walk serially.
disable-model-invocation: true
---

# To tickets

This is **Structure**, step 5 of the operator lock at `docs/HUMAN-LOOP.md` in the graphwing checkout (`repos.json` maps `graphwing` to its path). Read it if anything here is ambiguous. The lock wins.

Produce a **slice map**: an index JSON plus one markdown file per ticket, written into the app worktree. Graph walks it one ticket at a time. Graph never Structures. The engineer approves the map once, then fires the run.

Shortcut holds the inbound idea and nothing else. Do not mirror slices onto Shortcut cards, do not copy acceptance criteria there, do not apply tracker labels.

## Process

### 1. Work from the spec

Use the spec and the conversation already in context. If passed a path or story id, fetch and read it in full.

### 2. Read the code

Understand the current state before cutting. Ticket titles use the project's domain vocabulary and respect the ADRs covering the area. Look for prefactoring that makes the real change easy, and make it the first ticket.

### 3. Cut on behavior

Each slice is a **tracer bullet**: a narrow but complete path through every layer, demoable on its own.

Cut on behavior, never on budget. "It fits in one context window" is not a reason to split, and neither is `S`, `M`, or `L`. The engineer stamps size on the story, after the cut.

Per slice:

- 3 to 8 testable acceptance criteria. These are the contract, not Gherkin.
- One primary seam.
- A title needing "and", or covering two user journeys, is two tickets.

Nag the engineer when a slice carries more than 8 ACs, touches more than one primary seam, or will not fit `L`. They still make the call.

**Wide refactors are the exception.** One mechanical change whose blast radius fans across the codebase (rename a column, retype a shared symbol) cannot land green as a tracer bullet. Sequence it expand, migrate, contract: add the new form beside the old, migrate call sites in batches sized by blast radius with each batch its own ticket, then delete the old form in a ticket blocked by every batch.

### 4. Mark the fog

A slice you cannot write acceptance criteria for is not a build ticket. Give it `"kind": "decision"` and stop there. Graph parks on a decision ticket instead of guessing. After the engineer grills it, Structure adds the build tickets and the walk resumes.

Guessing here is the expensive failure. Parking is cheap.

### 5. Quiz the engineer

Present the breakdown as a numbered list before writing anything. Per ticket: title, blocked by, and the behavior it delivers.

Ask whether the granularity is right, whether each blocking edge genuinely gates, and what should merge or split. Iterate until they approve. They approve once, and then Graph owns the walk.

### 6. Write the map

Into the app worktree, under `slices/<story-slug>/`. Index at `slices/<story-slug>/index.json`.

## Index schema

```json
{
  "story": "SC-XXXXX",
  "test": "<tests.json recipe name>",
  "tickets": [
    { "id": "01-prefactor", "path": "slices/demo/01-prefactor.md",
      "blocked_by": [], "kind": "build", "status": "open" },
    { "id": "02-login", "path": "slices/demo/02-login.md",
      "blocked_by": ["01-prefactor"], "kind": "build", "status": "open" }
  ]
}
```

The server enforces these. Violating one returns 400 and the run never starts:

- `id` matches `^[a-z0-9][a-z0-9._-]{0,63}$`. Lowercase, starts alphanumeric, only dot, underscore, hyphen. No slashes.
- `id` is unique across the index.
- `path` is relative to the repo root, never absolute, never containing `..`.
- `kind` is `build` or `decision`. `status` is `open` or `done`.
- `blocked_by` is a list of ids, and every id in it exists in this index.
- `tickets` is non-empty.

Number ids in dependency order, blockers first, so the file reads in walk order.

Graph preserves keys beside `tickets` when it rewrites the index, so story id, test recipe, class, and size floor are safe at the top level.

Name the `tests.json` recipe that gates every slice. The spec supplies which one, because recipes belong to the project, not to this skill. A visual story with no named recipe has no per-slice gate, so give it one, a smoke test or a tiny happy path.

## Ticket file

The writer sees **only this file**. Not the grill, not the spec, not the other tickets. Every fact it needs to work is in here or it does not have it.

```
# <id>: <title>

**Behavior:** what works end to end when this is done, from the user's side.

**Seam:** the one place this change lives.

**Context:** the facts from the spec the writer needs. Self-contained.

## Acceptance criteria

- [ ] <testable criterion>
- [ ] <testable criterion>
```

Keep file paths and code snippets out; they go stale. The exception is a snippet that encodes a decision more precisely than prose can, a schema or a type shape, trimmed to the decision.

## Traps

**Cycles pass validation.** The server checks that every `blocked_by` id exists, never that the graph is acyclic. A cycle is not an error. Nothing is ever ready, the frontier comes back empty, and the walk ends silently as if the work were done. Walk the edges yourself before writing.

**Graph owns the index once the run starts.** `sliceComplete` flips `status` to `done` and rewrites the file. Do not hand-edit it mid-walk.

**The frontier is unblocked open `build` tickets.** A `decision` ticket parks the walker rather than advancing it.
