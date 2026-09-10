# Finishing a card — the goal it answers to, and who reviews it

Referenced from `SKILL.md`. Read this when taking a card in, or before declaring a card done.

A session's real capacity limit is the session itself — one card, or one `sert`-linked group of
cards, per session. `/chantier` operationalizes this natively instead of relying on judgment:

- Card's `sert: null` (a root) → `/goal` is set from **this card's own** `## Done when` line.
- Card's `sert: <parent>` → walk up to the root card (`sert: null`) that this chain ultimately
  serves, and set `/goal` from **the root's** `## Done when` line, not the leaf's. The session may then
  freely work any card in that `sert` group — the group is the unit of session, not the single card.

Consequence: a `## Done when` line must be **demonstrable in the transcript** — a test result, an exit
code, a real diff pasted — never merely true. `/goal`'s evaluator reads only the conversation
transcript, never a file or a command it would run itself; an assertion without the underlying
output in the transcript fools it exactly the way an unverified sub-agent report would.

### Reviewing before declaring a card done

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

### The review belongs in the `/goal` condition, not in an after-the-fact hope

The section above is a *should* that nothing enforced — an audit of closed cards found it skipped
on roughly half the judgment work, invisibly. So when a card's work needs judgment review, the
review goes **into the `/goal` condition itself**, set when the card is taken (`chantier` SKILL.md
step 7): the harness-native Stop-hook then holds the card open until the transcript shows the
review ran. Phrase it as a transcript-verifiable state, never a promise — e.g. append to the
`## Done when`-derived condition `… and the final diff has been reviewed by `adversarial` or
`Agent(reviewer)`, findings addressed, and the `- [x] reviewed (<method>)` box checked on the card`
— naming the checkbox so satisfying the goal and stamping the box are one action.

The judgment/mechanical call is made at intake, before the diff exists — so **when it is unclear,
include the clause**: a spurious clause on a card that turns out mechanical costs one
`- [x] reviewed (n/a — mechanical)` stamp, a missing clause on judgment work is the failure this
whole mechanism exists to stop. An *obviously* mechanical card (doc typo, rename, one-line config)
carries no clause and is closed `- [x] reviewed (n/a — mechanical)`, so the audit can tell "nothing
to review" from "review skipped".

`stop_handoff.py --list-unreviewed-done` (surfaced by `/chantier`'s intake) is the retroactive
net: `4-done` cards missing a checked `reviewed (...)` line, tagged `(no review line)` or
`(reviewed unchecked)`, most recent first, plus a count of cards closed `(n/a — mechanical)` so that
escape hatch's rate stays visible. It reaches `4-done` only — a card folded to `_ARCHIVE/` leaves
the net. Not a gate — a card is never blocked from `4-done` — just the end of the silence.

### Making the review visible on the card

Not every card goes through the section above — a purely mechanical change has nothing for a
reviewer to catch, and stamping one on every card regardless would turn a real judgment call into a
box-ticking ritual. When a card's work *did* need real judgment reviewed, make it visible on the
card itself: immediately above the `## Done when` line (already present by this point — it was set at
selection), add `- [ ] reviewed (method)`, `method` one of `adversarial`, `reviewer`,
`self-degraded`, comma-separated if more than one was actually used (e.g. `adversarial, reviewer`) —
or `n/a — mechanical` for a mechanical card that had nothing for a reviewer to catch.
Check it — `- [x] reviewed (…)` — once the review above is done, before declaring the card done.

Added by the session doing the work, never by `/chantier`'s intake (step 7). This checkbox is
descriptive, not a new gate: the review itself stays mandatory per the section above whenever real
judgment is involved, independent of whether the checkbox is present. What is *not* enforced is the
checkbox's own bookkeeping — no script blocks completion on a missing box. An omitted checkbox on a
`4-done` card is surfaced by `--list-unreviewed-done`, so the silence is visible even though it is
not blocking; the fix is one line (`reviewed (<method>)` or `reviewed (n/a — mechanical)`), not a
re-review.

### Proposing the next card, once the card is done

Closing a card is not the end of the session's job — the next card does not pick itself. Once
the `## Done when` state is verified, propose candidate cards for `2-selected-for-dev`; never move
one there without an explicit OK in the same exchange — the commitment column stays a human
decision (`.claude/rules/kanban-proactivity.md`).

**Candidates** — open cards in the same chantier's `1-todo/` (a card still in `0-ideas/` needs
scoping first, never proposed directly), filtered and ordered:

1. **Exclude** any card whose `blocked_by` points at a card not yet `done` — never propose a
   blocked card, however old. A card missing `date:` is malformed; flag it instead of guessing a
   sort position.
2. **Sort the rest into two buckets**, each by `date:` ascending: cards whose `blocked_by` was
   resolved in *this same session* first, then everything else. Both buckets are plain FIFO — the
   dependency filter already did the real work, this is only the tie-break.

**Nothing survives the filter** → say so plainly ("nothing to propose — `1-todo/` is empty or
everything is blocked") and propose nothing; do not lower the bar to fill a slot.

On explicit user OK: `mv` the chosen card into `2-selected-for-dev`. On silence or "not now": do
nothing — the proposal is not a commitment, and repeating it unprompted next turn is not this
skill's job either.

### Relaying a multi-repo card

A card with 2+ entries in `repos:` is worked by one worker session per repo, spawned by the
orchestrator through skill `spawn-workers` (one `cmux new-workspace` per repo). Most such cards are
the `pinky-agentic-plugins` + `pinky-bootstrap` pair, where the flow is **sequential** (bootstrap is
the vendored source, plugins the copy) — the orchestrator spawns only the first, and each worker
relays to the next once its own part of `## Done when` is reached:

- **Spawn the next repo** — `/pc-kanban:spawn-workers <chantier-id> <card-slug> <next-repo-slug>`
  (the single slug filter). Inside cmux/wmux it opens the workspace; outside any multiplexer it
  prints the `cd … && claude …` line to run. Or, if a worker for that repo is already listed by
  `ListAgents`, `SendMessage` it a bare trigger ("take over on `<card-slug>`").
- **Work state travels in the card's handoff block, never in the message.** The narrative handoff on
  the `3-in-progress` card is the single source of what is done and what is left; the inter-session
  message carries only the trigger. A relay that puts state in the message loses it the moment the
  receiving session compacts.
- The receiving worker runs `/pc-kanban:chantier`, finds the card already in `3-in-progress`
  (resume-directly branch), reads the handoff, and continues.

### Abandoning a multi-repo card

If a multi-repo card is dropped mid-flight (superseded, blocked, wrong approach):

1. Close every spawned worker's cmux workspace — a `new-workspace` with nothing running in it is not
   auto-reaped, and an orphan named `<repo>-<slug>` misleads the next `ListAgents`.
2. `mv` the card back to the column it came from (`2-selected-for-dev` or `1-todo`) — the column
   folder is the card's state, nothing else to reset.
3. Note the abandonment and why in the card's handoff block, so a later pickup does not re-derive
   the same dead end.
