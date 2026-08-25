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
mechanical check, prefer in this order — **never `haiku`, whatever tier is available**:

1. **`adversarial` MCP tool** (`pc-core`, native, no spawn) — a genuinely different model family
   (Mistral-served, `mistral-medium-3.5` today), not just a cheaper same-vendor tier. The real
   change of perspective, not a cost optimization.
2. **`Agent(subagent_type: "reviewer")`** — `pc-core`'s own agent (`plugins/pc-core/agents/
   reviewer.md`), preloads the `review-guidelines` skill, no external plugin dependency. Same
   vendor as the generator, but a fresh context that never saw the generating session's own
   rationalizations — `model: sonnet` in its own definition, a real floor.
3. **Same-session self-review** — last resort only, and **say plainly that it is degraded** (same
   model, same context) rather than presenting it as equivalent to (1) or (2).
