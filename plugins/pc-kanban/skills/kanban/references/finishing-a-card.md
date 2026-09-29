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

### Who writes the work

A worker session (`chantier` SKILL.md step 7's **Role** bullet) does not write a card's
implementation in its own turn. Real code changes go to `Agent(subagent_type:
"pc-kanban:card-worker")`, called directly by the worker session itself — **never** via the
`spawn-workers` skill, whose own Rule 1 forbids a worker from invoking it on its own repo (that
skill relays a card to a *different* repo it doesn't already occupy). The generated prompt still
needs `spawn-workers`'s fixed block — this repo's own conventions **probed from disk**, never
retyped from memory (`pc_kanban_spawn_workers.py::repo_conventions_block`: test location, test
command, gate command; a 2026-09-20 witness run measured exactly what a hand-typed version of this
missed) — plus the card's absolute path and `## Done when` as the generated block. The agent
inherits nothing from this session's context.

The loop this closes: worker → hand-back → review (below) → renvoi if KO → acceptance → gates/tests
green → `pc-git:commit`/`commit-and-pr` → card closed. The session that dispatched the worker never
writes the implementation itself and never commits mid-loop — a commit outside the `commit`/
`commit-and-pr` fork skips the gitmoji/commit-message convention those forks load. Two renvois
maximum: a worker that misses the same point twice on the same card is a brief problem, not a work
problem — stop and bring it to the user rather than trying a third time.

**Carve-out — a single mechanical fix stays in-session.** A narrow regex widened by one
alternative, a docstring correction, a single-file caller update with no design decision left open
— bounded the same way `chantier` SKILL.md's own "obviously mechanical" test is (a doc typo, a
rename, a one-line config change). Not the same rule as step 6's dispatch decision (that one picks
between an existing skill and building a new one when none fits; `card-worker` already exists and
already covers this shape) — a separate, narrower exception: spawning an agent for a one-line fix
costs more than it returns, so the session makes it directly instead.

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
   sort position. **Exception**: a `blocked_by` pointing at a card now `dropped` (§ below) is never
   excluded this way — it goes through the obsolescence check instead, because a dead
   blocker calls the blocked card's own premise into question, not just its availability.
2. **Sort the rest into two buckets**: cards whose `blocked_by` was resolved in *this same session*
   first, then everything else. Within each bucket, sort by `priority` (skill `kanban` § Card
   frontmatter) descending, `date:` ascending as tie-break at equal `priority`.

**Nothing survives the filter** → say so plainly ("nothing to propose — `1-todo/` is empty or
everything is blocked") and propose nothing; do not lower the bar to fill a slot.

On explicit user OK: `mv` the chosen card into `2-selected-for-dev`. On silence or "not now": do
nothing — the proposal is not a commitment, and repeating it unprompted next turn is not this
skill's job either.

### Flagging cards the closed card just made obsolete

The same moment — the closed card's own subject is now known in full — is the cheapest point to
catch another open card whose premise it just erased. List every other open card (`0-ideas/`,
`1-todo/`, `2-selected-for-dev/`) **in the same chantier** with the opening of its goal, not just
its title:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/kanban/scripts/open_cards_brief.py" <chantier-dir>
```

A title alone hides the overlap — "Worker = un agent" closed without flagging "Explorer `/goal` +
équipes d'agents", whose remaining goal it had just delivered. Read the just-closed card next to
that listing and judge whether any is now clearly superseded. Two triggers, same proposal:

- **Direct supersession** — the just-closed card's own content makes another open card's need moot.
- **Dead blocker** — a card `blocked_by` the just-closed card, where the closed card
  was itself `dropped` rather than finished `done`, is not silently unblocked and not left blocked
  forever: name it explicitly, next to the reason (its blocker is dead), same proposal turn as the
  next-card candidate above.

Name each candidate plainly ("`<slug>` looks superseded by what was just done — drop it?"); never
`mv`/drop without an explicit OK, same rule as the next-card proposal. On OK, drop it:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/drop_card.sh" <card-path> <reason...>
```

Lands the card in `_ARCHIVE/CHANTIER{NNNN}-dropped/`, never `-done/` (that name presupposes
finished work) — refuses a `4-done/` source, any other column moves, a reason is mandatory and
gets appended to the card's own body before the move. A `sert` chain whose parent is
now found `dropped` there degrades exactly like a parent "not found" (`chantier/SKILL.md` step 7):
say so plainly, append no `auto-parent` block, never invent one.

Record the check on the closed card, next to its `reviewed` line:
`- [x] obsolescence (none)`, or `- [x] obsolescence (<slug>, …)` naming the cards proposed for a
drop. Without it, "checked, nothing found" and "never checked" look the same.

**Nothing looks obsolete** → say nothing extra in the conversation; the `obsolescence (none)` line on
the card is the only trace, same discipline as the next-card proposal's own silence.

### Recommending session vs spawn, next to the proposal

**Worker sessions only** — an orchestrator never takes a card itself, so it never reaches this
step. Alongside the proposed card, run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/kanban/scripts/next_card_recommendation.py"
```

It prints `{"context_window_pct": <int|null>, "rate_limit": {"five_hour": {...}, "seven_day":
{...}}|null}`, each field independently `null` when its own data source is unavailable (missing
transcript, `pc-statusline` not installed) — never an error. Render the line from this output and
the candidate card's `repos:`/`sert` against the current session's own:

1. **Rate-limit override, checked first.** If either window in `rate_limit` has `triggered: true`,
   the whole line becomes a pause recommendation, naming the window and its `resets_at` — skip
   step 2 entirely. Do not recommend "continue in session" when the account is about to get cut
   off regardless of context headroom.
2. **Otherwise, context-window + proximity.** `context_window_pct` (`null` → omit the line
   entirely, nothing to base it on) against the `<50` / `≥50` band gives the default recommendation
   (continue / spawn a fresh session); a candidate sharing `repos:` or `sert` with the current
   session modulates the wording toward "continue" but never flips a `≥50` verdict to the
   opposite — this stays a recommendation, never a gate (`.claude/rules/kanban-proactivity.md`).

```
→ Proposition : <card-slug>
  💡 Reco : continuer en session (context à 22%, même repos:)
```
```
→ Proposition : <card-slug>
  ⚠️ Reco : attendre le reset (dans 1h15, five_hour à 96%) avant de démarrer — risque de hit
  mi-travail, cache froid à la reprise
```

Purely consultative end to end: this step never `mv`s the card, never spawns a session, never
forces a pause — the same human decision as the proposal itself.

### Naming the session, once the proposal is out

**Any session, worker or orchestrator.** A session working a `sert`-linked group takes several cards
in turn, so the name is set here, each time a card closes, never at card-take (a rename at each take
would overwrite the group's identity with the latest card). It comes from the cards **this session**
has closed so far, read from its own recent turn history:

- **One card closed** → `/rename <that card's slug>`.
- **Several, all serving the same `sert` root** → `/rename <root's slug>`.
- **Several, not all serving the same root** → `/rename <slug>_<slug>_<slug>_+N`: the first three
  closed slugs in closing order, then `+N` for the N remaining ones (omit `_+N` when there are three
  or fewer).
- **The closed set cannot be reconstructed** (history compacted or session resumed) → treat only the
  card just closed as the set.

`/rename` is parsed at the CLI level before the turn, exactly like `/goal` (`chantier` SKILL.md
step 7's **Goal** bullet) — this skill cannot run it: emit it as a pasteable line at the tail of
the response, after the recommendation above. Queue it with the one and only Bash call this step
makes, the last tool call of the turn (a standalone `require` here is safe — nothing follows it in
this section):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/pc_kanban_pending_user_action.py" require rename '/rename <name>' --surfaced
```

Only after that call returns, close the response with:

```
📛 Next step: rename this session.
/rename <name>
```

Add nothing past it — just the pasteable line, ready to paste as the next message.

### Touching a repo the card does not list

A worker is frozen to its card's `repos:` plus the boards. When the work turns out to need another
repo, never write across (`freeze off`, or a Bash write, silently drops the scope the card was
taken under). Instead:

1. Stop editing.
2. Add the repo's slug to the card's `repos:`, and say why in the handoff block.
3. Relay as for a multi-repo card (below): `/pc-kanban:spawn-workers <chantier-id> <card-slug>
   <repo-slug>`.

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
