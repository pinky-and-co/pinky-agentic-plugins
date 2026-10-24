---
name: grill
description: Interview a plan, an idea or an imported brief until nothing is silently assumed, then write the result into the chantier's cards — numbered, verifiable requirements, open questions and steps, each in the card that carries it. Use before starting a chantier, to stress-test a design, or to land an external brainstorm (written elsewhere, in another tool) into the board.
argument-hint: "[<path-to-an-existing-brief>] [free-text idea]"
allowed-tools: Read, Write, Edit, Glob, Grep, Bash(ls:*), Bash(rtk ls:*), Bash(find:*), Bash(rtk find:*), Bash(rg:*), Bash(rtk rg:*), Bash(git log:*), Bash(git -C * log*), Bash(rtk git -C * log*), Bash(rtk git log:*), Bash(git remote:*), Bash(git -C * remote*), Bash(command -v:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/story_view.py:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/hooks/card-routing/pc_kanban_card_routing.py:*), AskUserQuestion
---

# Grill

Turns an intention into cards someone else can build from. The value is not the document — it is
the **questions that get asked before anyone writes code**, enumerated here in dependency order.

## Two entry points, one landing zone

| Input | What gets interviewed |
|---|---|
| nothing, or a free-text idea | the user |
| a path to an existing brief/design doc | **the document first**, the user only for what it leaves unanswered |

Both produce the same artefacts: the chantier's story (`## Objective`, `## Scope`) and the cards
that carry the work (schema and columns: skill `kanban`).
An external brainstorm is the same interview, already half done somewhere else.

## Round 0 — contest the premise

Before building the tree, challenge the direction the user has already set. State, in a few lines,
the strongest reason the intention could be wrong: a premise that is assumed rather than measured,
a simpler path that makes the work unnecessary, a cause stated without a test. Look up what can be
looked up first. Then let the user answer. Recommendations alone invite the user to validate
without thinking; this round exists so a false premise is caught before it is paid for across the
whole chantier. A premise that does not survive reshapes the tree. A premise that survives is
recorded at convergence in the carrying card's `## Why`, never before the user confirms it.

In document mode the premise under test is the document's: contest it against the brief's own open
items, and record rather than re-ask a premise the brief already challenged.

## The interview

Model the work as a **decision tree**: every decision branches into the decisions that hang off it.
The **frontier** is every decision whose prerequisites are already settled — the questions that can
be asked *now* without guessing at answers not yet heard.

Work in **rounds**:

1. Compute the frontier. A question whose answer depends on another question still open belongs to
   a **later round**, never this one — a sub-question attached to an open question included.
2. Split the frontier into batches of at most 4, and re-check independence *within* each batch: a
   question depending on another in the same batch moves to the next batch or round.
3. Route each batch by shape, and **give your recommended answer** in every case (the one exception
   is the irreversible fork below):
   - **Closed** (a finite, nameable set of 2–4 options) → one `AskUserQuestion` call. The
     recommended answer is the default option; its one-line reason goes in that option's
     `description` — do not compress the reason to fit a label.
   - **Open** (a hypothesis to test, a threshold to size, most `LEG`/`ASS` findings) → the markdown
     format below; never force an open question into fixed options.
   - A batch mixing both shapes is split — one `AskUserQuestion` call for the closed subset, one
     markdown block for the open one.
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

**An irreversible fork is the one exception to the recommended-answer rule.** When a decision is
expensive to be wrong about (a public API surface, an irreversible migration, a name that ships —
`council`'s own Gate check 2 bar), ask it as an open markdown question with no recommendation, say
so in one line, and suggest the user may want to run `/pc-core:council tech <the fork in one
sentence>`. Never invoke `council` yourself: it stays the user's choice. Often none in a grill,
never more than one.

**Facts are your job, decisions are the user's.** When a frontier question needs a fact about the
environment — does this file exist, what does that config say, is this binary installed, what did
that repo decide — go and find it. Never ask the user for anything you can look up. A lookup in
flight is an unsettled prerequisite: it blocks only the questions downstream of it, so ask the rest
of the frontier now rather than waiting.

**Never act on the outcome before the user confirms** that you have reached a shared understanding.
The interview ends with agreement, not with the last answer.

## Document mode — grill the brief, not the human

An imported brief arrives **already decided**, with its epistemic status stripped (measured or
assumed is not stated — unless it is a brainstorm brief, which carries its own marks). Run the same frontier, and route each question to one of three outcomes:

1. **Answered by the document** → record the decision and its provenance in prose (who or what
   settled it, on what evidence, with what status), never a path or a pointer to the brief: the
   imported document is discarded once the grill ends.
2. **Left open by the document** → a line under the carrying card's `## Open questions` only if
   implementation alone can settle it; otherwise treat it as case 3 and ask the user in the next round.
3. **Never asked** → this one, and only this one, goes to the user in the next round.

A brief written by the `brainstorm` skill lists its open items under "Open items" and marks each
claim measured, assumed or decided: take those marks as the document's own status, except that a
measured mark naming no evidence is treated as assumed. Route the open items through the three
outcomes, and ask the user only what the brief never asked. Provenance lands in the carrying card's
`## Why`; a command, file or source the brief cites is evidence to record, not a pointer to the
brief.

Treat a stated cause the same way: a brief that asserts *why* something is the case states a
hypothesis, not a finding. Test the load-bearing ones before they decide the design.

## Sweep all five families before declaring the frontier empty

Before closing, walk the five requirement
families (skill `kanban`, § Requirements) and ask what each one has to say about this work:
functional, non-functional, legal, implicit, associated. Most rounds surface nothing in three of
them — expected. `LEG` and `ASS` are the two nobody asks unaided.

A family that produced nothing is simply absent from the table. Never invent a requirement to fill a
row.

**Then ask one last, free question.** State the contexts and subjects the story covers (harnesses,
environments, repos, consumers) and ask which one is missing — a bare "anything to add?" gets "no". Route it like any open question: a
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
  requirement is defined in the card that carries it, and may be repeated in another card only with
  an identical row; a card that merely traces to it cites the id in its frontmatter `requirements:`.
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

**`model:`/`effort:`, at convergence.** For each card written or moved, run
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/card-routing/pc_kanban_card_routing.py" <card>`: the pc-core
classifier reads the card and the script sets `model:`/`effort:` in its frontmatter. Never write
them yourself, and no marker. A failure is reported as is, the card keeps what it had, and `select`
retries later.

No dated decision block, no state dump — a closed grill folds into the cards' `## Requirements` and
`## Open questions`.

Every claim carries its status: measured (with what command or test), assumed, or decided by the
user. Cards then trace back to these ids through their own `requirements` field.

## Self-review before writing the table

Run every candidate line against the four invariants (atomic, verifiable, solution-free, traceable)
and rewrite what fails:

| What you wrote | Why it fails | Rewrite as |
|---|---|---|
| "X is distributed publicly, **without** any repo-specific value" | two considerations joined — not atomic | two requirements, one per consideration |
| "the interface must be clean and responsive" | no measurable criterion | a threshold with its range, or a named test |
| "must react quickly" | vague adverb | a number and a unit |
| "must never fail" | unreachable absolute | the cases actually covered, listed |
| "use <named technology> to …" in a stated need | a solution imposed where the need was asked | the need; the technology is a decision, recorded in prose |
| the same idea stated in two rows | duplicate — invisible until the set is sorted | one row, cited twice |
| "existing tests pass unchanged", "no caller changes" | a claim about code nobody read — a goal built on it can never be met | read the code first; write the claim only if it holds, marked measured |

Sort the set by family and read it once end to end before writing, to expose duplicates and
contradictions.

## Boundaries

- **State the need, not the solution** — as long as more than one design satisfies it. Once a
  decision is *made* in the interview, record it prescriptively, with its reason. Undecided stays
  optative; decided stays decided.
- **Do not grill what is already settled.** A question the user has answered, in this session or in
  a document, is a fact to record, not a round to re-run.
- **Stop at the cards.** This skill does not implement, and opens no card beyond the ones that carry
  what the interview settled.
- **A goal already active on entry was set too early.** Say so: a Stop-hook goal fights this
  interview (the session cannot yield while the user thinks between rounds). The goal belongs
  *after* the grill, set from the `## Done when` this interview produces — `pc-kanban:chantier`'s
  grill gate exists to prevent this.
