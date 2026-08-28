# Council type: `tech`

An architecture or technical choice where multiple credible approaches exist. Produces an ADR ready
to commit — not a report.

Use for: "which injection pattern for the runner?", "Pydantic or dataclasses for the models?", "store
results in the repo or in `.claude/`?". Not for non-technical decisions (→ `generic`), not for
whether to build a concept (→ `pitch`).

## Step 1 — extra context to scan

- `docs/adr/` of the target repo — read the existing ADRs: decision history, and what is already
  settled.
- the repo's `CLAUDE.md` / `AGENTS.md` — conventions, constraints, existing debt.
- any `CONTEXT.md` — canonical names, resolved ambiguities.
- relevant knowledge files for the stack in play.

Frame the question with: the choice to arbitrate (concrete options), known constraints (perf,
compatibility, existing debt), a summary of relevant existing ADRs, what a wrong call costs.

## Gate check 3 (the type's own)

**Read `docs/adr/` first.** If an accepted ADR already settles this → abort, cite the ADR, and say
what would have to change to reopen it. Re-litigating a settled decision is the most expensive noise.

## Prior-art focus (step 2)

Established practice or a package that already solves this: PyPI, GitHub (the platform's official org
especially), official platform docs, named patterns (CQRS, event sourcing, …) — cite the name and
whether it actually applies. Under 150 words: what exists, what's missing, what's been abandoned.

## Advisor framing (step 3)

The five lenses in a technical context: Contrarian hunts the failure point in the approach; First
Principles challenges the underlying assumption; Expansionist looks for the architectural
opportunity hiding behind the choice; Outsider has zero context on the codebase and reacts only to
what's presented; Executor asks only "implementable, and fastest path?".

## Chairman — extra sections (step 5)

None beyond the shared five — but `## The Recommendation` must land on one of the concrete options,
not a synthesis that dodges the choice.

## Step 7 — output

**`docs/adr/NNNN-<slug>.md`** in the target repo. Next number: `ls docs/adr/*.md | sort | tail -1`,
increment. Format (match the repo's existing ADRs if they differ):

```markdown
---
title: "NNNN"
---
# ADR-NNNN — <title>
*Update date : YYYY-MM-DD HH:MM*
Status: accepted

## Context
[the question as put to the council + key constraints]

## Options evaluated
[the debated options, one line each]

## Council verdict
[key convergence and divergence, including the cross-model check]

## Decision
[from the chairman's recommendation — one option, chosen]

## Consequences
[trade-offs accepted; what's gained, what's sacrificed]
```

Add a pointer in the repo's `docs/index.md` (or let its generator handle it on a mkdocs repo). Then
the transcript (always).
