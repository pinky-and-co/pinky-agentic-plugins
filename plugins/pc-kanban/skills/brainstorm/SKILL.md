---
name: brainstorm
description: Diverge before deciding — explore an idea with the user, name the concepts it hides, challenge generic claims, then hand back a brief where every claim carries its epistemic status, open items are listed and implementation steps are ordered. Use when starting from a vague idea, before any design is chosen, or to prepare a brief that a later requirements interview will consume.
---

# Brainstorm

Explores an idea widely and hands back a brief someone else can interrogate. It does **not**
converge on a design: choosing is the job of the interview that comes after. The value is the
questions this session asks that the user did not know existed.

## How to run it

Talk with the user, one thread at a time. Keep every move below in play; none is a fixed step.

- **Spot patterns.** When two things the user said share a shape, say so and ask whether the
  resemblance is real.
- **Hunt paradoxes.** Two wishes that cannot both hold are the most informative finding — surface
  them rather than smoothing one away.
- **Name the unnamed.** A recurring thing the user describes with a paragraph gets a short name,
  proposed and then confirmed or renamed by the user.
- **Contrast.** Describe the idea next to what it is not, or next to its nearest alternative, to
  show where its edges are.
- **Challenge generic claims.** "Simple", "scalable", "users want", "best practice", "obviously":
  each is a placeholder. Ask for the case, the number or the example behind it. A claim that stays
  generic after one challenge is recorded as assumed.

Offer several directions side by side; do not recommend one. If the user asks which to pick, give
the trade-offs and leave the choice with them.

## Epistemic status

Every claim in the Claims table carries exactly one status; an assertion made elsewhere in the
brief is added to the table or dropped:

| Status | Meaning |
|---|---|
| **measured** | evidence named: a command, file, test, cited source, or an observation made in this session |
| **assumed** | believed, not checked — the default for anything without evidence |
| **decided** | chosen by the user, with their reason |

Never upgrade a claim: something the user states confidently without evidence is assumed.
A stated cause (why something is the way it is) is a hypothesis, so it is assumed until measured.

## What it hands back

Write one brief, in this order:

1. **Intent** — what the user wants and why, in a few lines.
2. **Concepts** — the named things the session surfaced, one line each.
3. **Claims** — a table: claim, status (measured / assumed / decided), evidence or reason.
4. **Items ouverts** — the open items: questions nobody answered, paradoxes unresolved, assumed
   claims that the design would rest on. Each says what would settle it. Never empty by default:
   an empty list means the challenge did not happen.
5. **Implementation steps, ordered** — what has to exist first for the rest to be testable, in
   dependency order. Steps describe work, not a chosen design: a step that names a technology,
   protocol or service is rewritten as the capability it enables.

Write the section heading "Items ouverts" exactly so, whatever language the rest is written in:
the interview that consumes this brief looks for it.

## Boundaries

- Do not pick a design, a stack or an architecture. Present them as open items.
- Do not invent evidence to fill the status column.
- Stop at the brief. Planning and building come after, in another session.
