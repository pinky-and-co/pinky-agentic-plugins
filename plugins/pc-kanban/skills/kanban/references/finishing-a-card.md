# Finishing a card — the goal it answers to, and who reviews it

Referenced from `SKILL.md`. Read this when taking a card in, or before declaring `Fini =` met.

A session's real capacity limit is the session itself — one card, or one `sert`-linked group of
cards, per session. `/chantier` operationalizes this natively instead of relying on judgment:

- Card's `sert: null` (a root) → `/goal` is set from **this card's own** `Fini =` line.
- Card's `sert: <parent>` → walk up to the root card (`sert: null`) that this chain ultimately
  serves, and set `/goal` from **the root's** `Fini =` line, not the leaf's. The session may then
  freely work any card in that `sert` group — the group is the unit of session, not the single card.

Consequence: a `Fini =` line must be **demonstrable in the transcript** — a test result, an exit
code, a real diff pasted — never merely true. `/goal`'s evaluator reads only the conversation
transcript, never a file or a command it would run itself; an assertion without the underlying
output in the transcript fools it exactly the way an unverified sub-agent report would.

### Reviewing before declaring `Fini =` met

The reviewer must not be the generator — a same-model self-check inherits its own blind spots, and
agreement among several voices of the same model is not proof of correctness either, only a
correlated blind spot repeated. Whenever a card's work needs real judgment reviewed, not just a
mechanical check, prefer in this order:

1. **`adversarial` MCP tool** (`pc-core`, native, no spawn) — the default. Routes the review to a
   non-Claude pool model, not a cheaper same-vendor tier: the real change of perspective. Pass
   `diff` (a path to the change under review) so the tool picks the model tier deterministically
   from the diff's size and risk factors — a security-sensitive or large change goes opus-tier, a
   small mechanical one to the cheapest capable entry. Pass `stack`, or let it infer one from the
   imports, so a FastAPI / SiS / Snowflake checklist rides along (the pool model has no `Skill`
   tool). Falls through pool candidates on failure; returns `[adversarial] BLOCKED — …` when no
   pool key resolves at all — that is the signal to drop to (2).
2. **`Agent(subagent_type: "reviewer")`** — `pc-core`'s own agent (`plugins/pc-core/agents/
   reviewer.md`), preloads the `review-guidelines` skill, no external plugin dependency. The
   fallback when (1) returns `BLOCKED`, and the right first choice when the review needs a reviewer
   that navigates the repo itself rather than a single-shot pool call. Same vendor as the
   generator, but a fresh context that never saw the generating session's own rationalizations —
   `model: sonnet` in its own definition, a real floor.
3. **Same-session self-review** — last resort only, and **say plainly that it is degraded** (same
   model, same context) rather than presenting it as equivalent to (1) or (2).

### Making the review visible on the card

Not every card goes through the section above — a purely mechanical change has nothing for a
reviewer to catch, and stamping one on every card regardless would turn a real judgment call into a
box-ticking ritual. When a card's work *did* need real judgment reviewed, make it visible on the
card itself: immediately above the `## Fini =` line (already present by this point — it was set at
selection), add `- [ ] reviewed (method)`, `method` one of `adversarial`, `reviewer`,
`self-degraded`, comma-separated if more than one was actually used (e.g. `adversarial, reviewer`).
Check it — `- [x] reviewed (…)` — once the review above is done, before declaring `Fini =` met.

Added by the session doing the work, never by `/chantier`'s intake (step 7). This checkbox is
descriptive, not a new gate: the review itself stays mandatory per the section above whenever real
judgment is involved, independent of whether the checkbox is present. What is *not* enforced is the
checkbox's own bookkeeping — no script blocks `Fini =` on a missing box, so an omitted checkbox
makes a past review harder to audit later, it does not mean the review was skipped or optional.

### Proposing the next card, once `Fini =` is met

Closing a card is not the end of the session's job — the next card does not pick itself. Once
`Fini =` is verified, propose candidate cards for `2-selected-for-dev`; never move one there
without an explicit OK in the same exchange — the commitment column stays a human decision
(`.claude/rules/kanban-proactivity.md`).

**Candidates** — open cards in the same chantier's `1-todo/` (a card still in `0-ideas/` needs
cadrage first, never proposed directly), filtered and ordered:

1. **Exclude** any card whose `blocked_by` points at a card not yet `done` — never propose a
   blocked card, however old. A card missing `date:` is malformed; flag it instead of guessing a
   sort position.
2. **Sort the rest into two buckets**, each by `date:` ascending: cards whose `blocked_by` was
   resolved in *this same session* first, then everything else. Both buckets are plain FIFO — the
   dependency filter already did the real work, this is only the tie-break.

**Nothing survives the filter** → say so plainly ("rien à proposer — `1-todo/` est vide ou tout est
bloqué") and propose nothing; do not lower the bar to fill a slot.

On explicit user OK: `mv` the chosen card into `2-selected-for-dev`. On silence or "not now": do
nothing — the proposal is not a commitment, and repeating it unprompted next turn is not this
skill's job either.
