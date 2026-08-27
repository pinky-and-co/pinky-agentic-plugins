# Council type: `pitch`

Whether to build a product concept — validated **before any code**. Produces the founding
explanation docs for the repo: the "why" that rarely changes once written.

Use for: "pinky-check as a standalone validation tool", "an MCP server exposing knowledge files to
other agents", "a dry-run CLI for the framework". Not for a technology choice (→ `tech`), not for a
detail that affects one function or screen.

## Step 1 — extra context to scan

- workspace `CLAUDE.md` — active constraints (personal-leverage priority, YAGNI filter).
- nearby `docs/adr/` entries for related decisions.
- context files for the concept's domain.

Frame with: the concept in one sentence (what it is + who it's for), the problem it solves
(concretely, for whom, when), what already exists that might overlap, why it's worth arbitrating now.

## Gate check 3 (the type's own)

**Concept-level, not detail-level.** If the answer would only change one function or one screen — not
`1-design.md` or `2-features.md` — abort. And if *whether to build it* is settled and only the
concept's shape is open, that's still in scope; if the concept itself isn't worth building, the
council will say so.

## Prior-art focus (step 2)

Does this concept already exist, and in what form? Similar OSS tools (PyPI, GitHub), native platform
features that partially cover the need, projects that could be extended instead of starting new.
Under 150 words. "No relevant prior art" is itself a useful signal — say it explicitly.

## Advisor framing (step 3)

Contrarian looks for the problem the concept claims to solve but doesn't; First Principles asks what
the actual need is (the concept may be the wrong answer to the right question); Expansionist looks
for the underestimated 10× upside; Outsider has no context on project or stack and catches unspoken
jargon; Executor asks for the minimum version that proves the value and how fast it ships.

## Chairman — extra sections (step 5)

Add, after `## The Recommendation`:

```
## Verdict
[Go / No-Go / Conditional Go — if conditional, the condition stated explicitly]
```

## Step 7 — output

**If Go or Conditional Go**, write both in the target repo (propose the path if the repo doesn't
exist yet):

`explanation/1-design.md` — Positioning (what it is / is not / who for) · Problem solved (as the
council converged it) · Architecture (high-level + key decisions) · Rejected alternatives (what the
council ruled out and why) · Accepted trade-offs. A Conditional Go states the condition here.

`explanation/2-features.md` — Stable features (delivered, descending priority) · Planned features
(not built, priority order).

Both get an `*Update date : YYYY-MM-DD HH:MM*` line under the H1 and a pointer in `docs/index.md`.

**If No-Go**, write a short decision note in `docs/adr/` (an ADR marked `rejected`) explaining why
the concept was turned down.

Then the transcript (always).
