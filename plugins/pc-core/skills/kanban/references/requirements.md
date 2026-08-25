# Requirements — the norm a story's `## Exigences` table follows

Referenced from the `exigences` field in `SKILL.md`. Read this when writing or reading a
chantier story's requirements table; the method for *finding* requirements is the skill `grill`.

Cards trace to requirements; this is what a requirement has to be for the trace to mean anything.
The method for *finding* them is the skill `grill` — this section is only the written form.

**Four invariants. A line failing any one is rewritten, not filed.**

| Invariant | Test |
|---|---|
| **Atomic** | one capability per requirement — an "and", an "or" or a "without" joining two considerations is two requirements |
| **Verifiable** | a test, an inspection or a command can settle it; no criterion means no requirement |
| **Solution-free** | it states the need while more than one design satisfies it — once a design is *chosen*, that is a decision recorded in prose, not a requirement retrofitted to what got built |
| **Traceable** | a unique id, and cards pointing at it through `exigences` |

**Id: `{FAM}-{NNNN}-{NN}`** — family, the chantier's number, then a sequence **counted per family**
and never reused (a deleted requirement's number stays burnt, so an old card citing it stays
readable as an obsolete reference rather than silently pointing at something else).

| `FAM` | Family | What it catches that the others miss |
|---|---|---|
| `FR` | Functional | an expected service — data, behaviour, business rule |
| `NFR` | Non-functional | a quality: reliability, extensibility, reusability, portability, verifiability, efficiency |
| `LEG` | Legal | personal-data law, copyright, statutory duties — externally imposed, not negotiable |
| `IMP` | Implicit | what every practitioner of the domain knows and nobody writes down, platform facts included |
| `ASS` | Associated | development, delivery, training, maintenance, and **decommissioning** |

The family is a classification aid — it makes duplicates and conflicts visible across a set, and it
gives `grill` five angles to sweep. It is not a filing duty: a family that produced nothing is absent
from the table, and no requirement is invented to fill a row.

**Table columns: `Id` · `Priorité` · `Énoncé` · `Critère de vérification`.**

- **Priorité** — `MUST` (the chantier fails without it) or `SHOULD` (wanted, tradeable). Two values
  only; a third turns the column into a debate.
- **Énoncé** — one active sentence, the actor named, one capability. No vague adjective or adverb
  ("fast", "user-friendly"), no unreachable absolute ("always", "zero bug"), no open end ("etc.",
  "and/or").
- **Critère de vérification** — measurable and objective: a command, a test path, a threshold with
  its range. "Verified by inspection" is acceptable; "obviously true" is not.

The **rationale** — why the requirement is necessary — belongs in the story's prose above the table,
never inside the statement: mixing them makes the statement untestable and the reason unfindable.
