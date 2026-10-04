---
name: grill
description: Interview a plan, an idea or an imported brief until nothing is silently assumed, then write the result into the chantier's cards — numbered, verifiable requirements, open questions and steps, each in the card that carries it. Use before starting a chantier, to stress-test a design, or to land an external brainstorm (written elsewhere, in another tool) into the board.
argument-hint: "[<path-to-an-existing-brief>] [free-text idea]"
allowed-tools: Read, Write, Edit, Glob, Grep, Bash(ls:*), Bash(find:*), Bash(rg:*), Bash(git log:*), Bash(git remote:*), Bash(command -v:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/story_view.py:*), AskUserQuestion
---

# Grill

Turns an intention into cards someone else can build from. The value is not the document — it is
the **questions that get asked before anyone writes code**. Someone who has designed systems for
years asks them without noticing; someone who has not does not know they exist. This skill
enumerates them in dependency order so that ignorance of the question is no longer the failure mode.

## Two entry points, one landing zone

| Input | What gets interviewed |
|---|---|
| nothing, or a free-text idea | the user |
| a path to an existing brief/design doc | **the document first**, the user only for what it leaves unanswered |

Both produce the same artefacts: the chantier's story (`## Objective`, `## Scope`) and the cards
that carry the work (schema and columns: skill `kanban`).
An external brainstorm is not a rival input format — it is the same interview, already half done
somewhere else.

## The interview

Model the work as a **decision tree**: every decision branches into the decisions that hang off it.
The **frontier** is every decision whose prerequisites are already settled — the questions that can
be asked *now* without guessing at answers not yet heard.

Work in **rounds**:

1. Compute the frontier. A question whose answer depends on another question still open belongs to
   a **later round**, never this one — and this includes a sub-question annexed to another one: if
   it depends on its parent's answer, being attached to an already-open question does not exempt it.
2. Split the frontier into batches of at most 4, and re-check independence *within* each candidate
   batch: if a question depends on another question in the same batch, being asked in the same tool
   call does not make them simultaneous — move the dependent one to the next batch or round.
3. Route each batch by shape, and **give your recommended answer** in every case — a question
   without a default is a question the user has to design from scratch, which defeats the purpose:
   - **Closed** (a finite, nameable set of 2–4 options) → one `AskUserQuestion` call. The
     recommended answer is the default option; its one-line reason goes in that option's
     `description` — do not compress the reason to fit a label.
   - **Open** (a hypothesis to test, a threshold to size, most `LEG`/`ASS` findings) → the markdown
     format below. Forcing an open question into fixed options destroys the answer as much as
     answering it yourself would.
   - A batch mixing both shapes is split accordingly — one `AskUserQuestion` call for the closed
     subset, one markdown block for the open one — never force an open question in just to keep a
     batch homogeneous.
4. Stop and wait. The answers reshape the tree: settled decisions push the frontier outward. If
   `AskUserQuestion` returns an empty or unusable answer, that is not a silent yes — re-ask the same
   question in the next round rather than proceeding on it.
5. Repeat until the frontier is empty.

Format an open-question round like this:

```
❓ **Q1 — <title>**
<the question; state the options when there are options>

➡️ **Recommended:** <your answer, and the one-line reason>

---

❓ **Q2 — <title>**
…
```

**Facts are your job, decisions are the user's.** When a frontier question needs a fact about the
environment — does this file exist, what does that config say, is this binary installed, what did
that repo decide — go and find it. Never ask the user for anything you can look up. A lookup in
flight is an unsettled prerequisite: it blocks only the questions downstream of it, so ask the rest
of the frontier now rather than waiting.

**Never act on the outcome before the user confirms** that you have reached a shared understanding.
The interview ends with agreement, not with the last answer.

## Document mode — grill the brief, not the human

An imported brief arrives **already decided**, but with its epistemic status stripped: nothing says
which claims were measured and which were assumed, and the questions nobody asked leave no trace.
So run the same frontier, and route each question to one of three outcomes:

1. **Answered by the document** → record the decision *and where in the document it came from*.
2. **Left open by the document** → a line under the carrying card's `## Open questions` only if
   implementation alone can settle it; otherwise treat it as case 3 and ask the user in the next round.
3. **Never asked** → this one, and only this one, goes to the user in the next round.

Treat a stated cause the same way: a brief that asserts *why* something is the case states a
hypothesis, not a finding. Test the load-bearing ones before they decide the design.

## Sweep all five families before declaring the frontier empty

A decision tree only grows branches someone thought of. Before closing, walk the five requirement
families (skill `kanban`, § Requirements) and ask what each one has to say about this work:
functional, non-functional, legal, implicit, associated. Most rounds surface nothing in three of
them — that is the expected result, and it is cheap. What it buys is the fourth: `LEG` and `ASS` are
the two nobody asks unaided, and "what happens when this is retired" has no other moment to be asked.

A family that produced nothing is simply absent from the table. Never invent a requirement to fill a
row.

**Then ask one last, free question.** State the contexts and subjects the story covers (harnesses,
environments, repos, consumers) and ask which one is missing. A bare "anything to add?" gets "no",
and a context written nowhere is one only the user can name. Route it like any open question: a
markdown block in markdown mode; in `AskUserQuestion` mode, a final question with "Nothing is
missing" and "Something is missing", the detail coming through the free-text answer. If the answer
opens a branch, run its rounds, then ask once more.

## What the interview writes

The story keeps only `## Objective` (one line, and the constraint that shapes everything else) and
`## Scope` (skill `kanban` § The story file → `references/story-template.md`). Everything the
interview settles about the work lands in **the card that carries it** (skill `kanban` § Card body):

- **`## Requirements`** — the table, in the form the skill `kanban` § Requirements defines: id
  `{FAM}-{NNNN}-{NN}`, priority, statement, verification criterion. That norm is authoritative; do
  not restate or bend it here. Take each new id from
  `${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/story_view.py --next-id <FAM> <chantier-dir>`. A
  requirement is defined in one card only; another card that traces to it cites the id in its
  frontmatter `requirements:`.
- **`## Open questions`** — empty by default: the grill exists to close every question. An entry is
  allowed only when the answer can come solely from implementing, and it names what the
  implementation will reveal. A question the user can answer now is asked in a round, never
  written down.
- **`## Steps`** — what has to exist first for the rest to be testable. An order *between* cards is
  a `blocked_by`, never a list.

An item no existing card carries gets a card of its own — `0-ideas` if it is not scoped yet. Nothing
is left in the story for lack of a card.

**The source card**, when the input was an existing `0-ideas` card (not free text, not an imported
document), moves to `1-todo` on convergence — `date:` set to today, its `## Requirements` written and
`requirements:` listing the ids it now traces to. Never `drop` it: `kanban`'s drop mechanism is for a card confirmed
obsolete, never for one whose own open questions the interview just closed. Document-mode input has
no source card to move.

No dated decision block, no state dump — a closed grill folds into the cards' `## Requirements` and
`## Open questions`.

Every claim carries its status: measured (with what command or test), assumed, or decided by the
user. Cards then trace back to these ids through their own `requirements` field.

## Self-review before writing the table

Run every candidate line against the four invariants (atomic, verifiable, solution-free, traceable)
and rewrite what fails. The failures are predictable — these are the ones that actually occur:

| What you wrote | Why it fails | Rewrite as |
|---|---|---|
| "X is distributed publicly, **without** any profile-resolved value" | two considerations joined — not atomic | two requirements, one per consideration |
| "the interface must be clean and responsive" | no measurable criterion | a threshold with its range, or a named test |
| "must react quickly" | vague adverb | a number and a unit |
| "must never fail" | unreachable absolute | the cases actually covered, listed |
| "use <named technology> to …" in a stated need | a solution imposed where the need was asked | the need; the technology is a decision, recorded in prose |
| the same idea stated in two rows | duplicate — invisible until the set is sorted | one row, cited twice |

Sort the set by family and read it once end to end before writing: duplicates and contradictions are
visible in a sorted set and invisible in the order they were thought of.

## Boundaries

- **State the need, not the solution** — as long as more than one design satisfies it. Prescribing
  one implementation removes the chance of a better one being found, and a requirement retrofitted
  to whatever got built is not a requirement.
  The exception is deliberate: once a decision is *made* in the interview, it is recorded
  prescriptively, with its reason. Undecided stays optative; decided stays decided.
- **Do not grill what is already settled.** A question the user has answered, in this session or in
  a document, is a fact to record, not a round to re-run.
- **Stop at the cards.** This skill does not implement, and opens no card beyond the ones that carry
  what the interview settled.
- **A goal already active on entry was set too early.** Say so: a Stop-hook goal fights this
  interview (the session cannot yield while the user thinks between rounds). The goal belongs
  *after* the grill, set from the `## Done when` this interview produces — `pc-kanban:chantier`'s
  grill gate exists to prevent this.
