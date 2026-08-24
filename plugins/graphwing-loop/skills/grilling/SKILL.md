---
name: grilling
description: Grill the user relentlessly about a plan, decision, or idea. Use when the user wants to stress-test their thinking, or uses any 'grill' trigger phrases.
---

Interview the user relentlessly until you reach shared understanding. Map it as a **design tree**: every decision branches into the decisions hanging off it.

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites are already settled, the questions you could ask now without guessing at answers you have not heard. Do not drain the frontier. Ask the one question that reshapes the most of it, then wait.

Relentless means depth per question, not question count. Five questions where four are already answered is padding, and the user reads it as theater.

## Live or settled

Every candidate question is **live** or **settled**. Only live questions go in a round.

A question is live when two answers both survive the evidence and lead to different work.

Settle it yourself when any of these hold:

- **The user already answered it.** Reading their own words back to them is the worst version of this.
- **An artifact decides it.** The repo, a lock file, a doc, config, `--help` output. Go read it.
- **One answer is right and you know why.** A recommendation with no surviving alternative is a statement, so state it.
- **You invented the alternative.** Widening scope past what the user asked, then asking which width they meant, is a detour wearing a question mark.

Before a round ships, take each candidate question and name the artifact that would settle it. Read that artifact. What survives the read is live. A round of five where four are settled is a round of one.

Finding facts is your job, never the user's. Dispatch a sub-agent when the dig is wide. Only the questions downstream of that dig wait for it, so ask the rest now. The **decisions** are the user's. Put each one to them and wait.

## Shape before detail

A live question either sets the **shape** or fills in **detail**. Shape questions change what the later questions are. Detail questions fill a blank in a shape already agreed.

Rank the live frontier by blast radius: how much of the remaining tree this answer redraws. Ask the top one, alone.

Two questions share a round only when neither answer can change the other. That is rarer than it looks. If you are unsure, it is detail, and it waits.

Detail asked early is usually wasted. The shape answer often deletes the question outright, and the user spent a decision on something that no longer exists.

## Report what you settled

Killing a question does not mean hiding the decision. Open every round with the settled list: the call, and the evidence behind it. Two lines each.

The user overturns any of them by saying so. A silent assumption fails worse than a theater question. The settled list is what keeps a shrinking question count honest.

## Round format

Plain markdown, sentence case, no emoji. Apply `unslop`.

```
**Settled**
- <the call>. Why: <path, command output, or the user's own words>.

**<title>**
<body. Name the real alternatives and what each one costs.>
Recommend: <answer, and the reason>
```

One question is the normal round. Zero means the frontier is empty, so say so and stop. If you are about to write a third, you have stopped ranking by blast radius.

Each answer reshapes the tree. Settled decisions push the frontier outward and unblock what depended on them. Recompute, re-rank, go again.

## Done

The frontier is empty when you have visited every branch and assumed nothing. Do not act on the plan until the user confirms.

## Riftwing and graphwing ideas

Operator lock: `docs/HUMAN-LOOP.md` in the graphwing checkout (`repos.json` maps `graphwing` to its path). This pane interviews and specs. It does not implement.

Close the grill by stamping two things:

- **class**: `mechanical`, `visual`, or `sensitive`. Absent class means `mechanical`.
- **size floor**: `S`, `M`, or `L`. Yours to set. Graph may bump one step up from countable features, never down, never across class, never because tests are red.

Then hand off with `rewst-graph-handoff`. No `/implement`, `/build`, `/tdd`, or product edits in this pane.
