---
name: to-spec
description: Turn the grilled conversation into a spec on a Shortcut story. No interview, just synthesis. Stamps class and size floor, then moves the story to Ready.
disable-model-invocation: true
---

# To spec

Step 3 of the operator lock at `docs/HUMAN-LOOP.md` in the graphwing checkout (`repos.json` maps `graphwing` to its path). Read it if anything here is ambiguous. The lock wins.

Synthesize what the grill already settled into a spec, and put it on a Shortcut story. Do **not** interview. If the design tree still has live questions, go back to `grilling`.

Shortcut mechanics belong to the project's `shortcut` skill: workflow states, labels, teams, branch naming. Use it. Do not restate its conventions here, and do not hardcode a state name it discovers at runtime.

## The card is the idea

The Shortcut story is the **inbound idea**, an external handle for people who are not watching the run. It is not the implementation queue.

On the card: the problem, the solution, the decisions, the seams, what is out of scope.

Off the card: per-slice acceptance criteria, the ticket list, `blocked_by` edges, anything that changes as the walk progresses. Those land in the worktree at `to-tickets` time and stay there.

The test: if it changes while Graph is walking, it does not go on the card.

## Process

### 1. Read the code

Understand the current state before writing, if the grill did not already. Use the project's domain vocabulary throughout, and respect the ADRs covering the area you are touching.

### 2. Settle the seams

Sketch the seams where this gets tested. Prefer seams that already exist, and use the highest one available. Fewer is better, and one is ideal.

Confirm the seams with the engineer before publishing. `to-tickets` enforces one primary seam per slice, so a spec that leaves them vague pushes the problem downstream into a map nobody can cut cleanly.

### 3. Write the spec

Use the template below.

### 4. Carry class and size floor forward

Hold both from the grill and hand them to `to-tickets`, which writes them into `index.json`:

- **class**: `mechanical`, `visual`, or `sensitive`. Absent means `mechanical`.
- **size floor**: `S`, `M`, or `L`.

They pick the writer model and the reviewer at run time, so they have to survive from the grill to the run payload. `index.json` is where they survive.

Keep them off the card. They are graphwing routing metadata, they mean nothing to anyone else reading the tracker, and a second copy nobody updates goes stale.

### 5. Move to Ready

Ready means the spec is good enough to Structure. Nothing writes code yet.

Use the `shortcut` skill to move it. New stories land in the default state, so this is a create followed by an update.

## Spec template

```
## Problem

The problem the user has, from the user's side.

## Solution

What changes for them, from the user's side.

## User stories

As an <actor>, I want <feature>, so that <benefit>.

Cover the shape of the idea. Stop when you have covered it.
This is not the acceptance-criteria list and not the slice list.

## Decisions

What the grill settled: modules and interfaces touched, schema changes,
API contracts, architectural calls, and the clarifications the engineer gave.

## Seams

Where this gets tested, and why those seams over new ones.

## Testing

What a good test looks like here, which modules get tested, and the prior
art in the codebase to follow.

## Out of scope

What this deliberately does not do.
```

Keep file paths and code snippets out of the spec; they go stale fast. The exception is a snippet from a prototype that encodes a decision more precisely than prose can, a schema or a type shape, trimmed to the decision.

## Traps

**No acceptance criteria on the card.** They are per slice, they live in the ticket files, and mirroring them here creates a second copy that drifts the moment Graph starts walking.

**No slice DAG on the tracker.** Shortcut workflow columns never mirror `blocked_by` edges. The map in the worktree is the only dependency graph.

**Ready is a gate, not a start.** Graph does not write at Ready. The engineer still runs `to-tickets` and approves the map before anything fires.
