# Finishing a card — the goal it answers to, and who reviews it

Referenced from `SKILL.md`. Read this when taking a card in, or before declaring a card done.

## Contents

- Who writes the work
- Reviewing before declaring a card done
- The review belongs in the `/goal` condition, not in an after-the-fact hope
- Making the review visible on the card
- Proposing the next card, once the card is done
- Flagging cards the closed card just made obsolete
- Archiving a parent whose children are still open
- Recommending session vs spawn, next to the proposal
- Naming the session, at the end of the session only
- Touching a repo the card does not list
- Relaying a multi-repo card
- Abandoning a multi-repo card

One card, or one `sert`-linked group of cards, per session. `/chantier` sets `/goal`:

- Card's `sert: null` (a root) → `/goal` is set from **this card's own** `## Done when` line.
- Card's `sert: <parent>` → walk up to the root card (`sert: null`) that this chain ultimately
  serves, and set `/goal` from **the root's** `## Done when` line, not the leaf's. The session may then
  freely work any card in that `sert` group — the group is the unit of session, not the single card.

A `## Done when` line must be **demonstrable in the transcript** — a test result, an exit code, a
real diff pasted — never merely true. `/goal`'s evaluator reads only the transcript, never a file or
a command it would run itself.

### Who writes the work

A worker session (`chantier` SKILL.md step 7's **Role** bullet) does not write a card's
implementation in its own turn. Real code changes go to `Agent(subagent_type:
"pc-kanban:card-worker")`, called directly by the worker session itself — **never** via the
`spawn-workers` skill, whose own Rule 1 forbids a worker from invoking it on its own repo (that
skill relays a card to a *different* repo it doesn't already occupy). The generated prompt still
needs `spawn-workers`'s fixed block — this repo's own conventions **probed from disk**, never
retyped from memory (`pc_kanban_spawn_workers.py::repo_conventions_block`: test location, test
command, gate command) — plus the card's absolute path and `## Done when` as the generated block. The agent
inherits nothing from this session's context.

The loop this closes: worker → hand-back → review (below) → send-back if KO, routed per `chantier`
SKILL.md step 8 → acceptance → gates/tests green → `pc-git:commit`/`commit-and-pr` → card closed.
The session that dispatched the worker never writes the implementation itself and never commits
mid-loop — a commit outside the `commit`/`commit-and-pr` fork skips the gitmoji convention those
forks load. Two send-backs maximum: a worker that misses the same point twice on the same card is a
brief problem — stop and bring it to the user rather than trying a third time.

**Carve-out — a single mechanical fix stays in-session.** A narrow regex widened by one
alternative, a docstring correction, a single-file caller update with no design decision left open
— bounded like `chantier` SKILL.md's "obviously mechanical" test (a doc typo, a rename, a one-line
config change). Separate from step 6's dispatch decision: the session makes it directly.

### Reviewing before declaring a card done

The reviewer must not be the generator, and several voices of the same model are one correlated
blind spot. Whenever a card's work needs real judgment reviewed, in this order:

1. **A change with a diff: call the `adversarial` MCP tool directly**
   (`mcp__plugin_pc-core_pc-core-models__adversarial`) — a non-Claude pool model of `sonnet` tier
   or above, the real change of perspective; no subagent is spawned to relay it. Pass:
   - `base` — the branch the work forked from (`main`; `HEAD` for work not yet committed), so the
     tool computes the diff itself;
   - `tools: ["read", "grep"]` — without them the model cannot open a file and answers with a tool
     call written as text, which is no review;
   - `task` — the card's `## Why` and `## Done when`, copied, nothing else: never a narrative of the
     work, which would hand the reviewer the generating session's own rationalizations.

   The tool already applies the review methodology and the stack checklist. Its answer opens with
   `[answered by <label>]`: stamp `Method: adversarial (<label>)` on the card, the label copied
   whole, and relay the verdict as is.
2. **`Agent(subagent_type: "reviewer")`** — `pc-core`'s own agent (`plugins/pc-core/agents/
   reviewer.md`), launched only for a claim with no diff, or when `adversarial` answered `BLOCKED`
   or `ERROR` (`pc-core`'s `reviewer_gate.py` hook enforces it, denying the `reviewer` call on a
   diff until such an answer is in the session). It reviews in a fresh context under the
   `review-guidelines` skill, and opens its report with `Method: reviewer (<model>)`, the model that answered. Pass it its own frontmatter
   defaults (`model: sonnet`, `effort: high`).

   When the card declares `mockup:`, the `task` (or the reviewer's hand-off) also carries the
   mockup path (the unpacked folder for a `.zip`, the sibling `<zip name>/`) and the card's `mockup_fidelity` levels, an axis
   not listed counting as `guided`. The review checks the fidelity axis by axis: `strict` is
   reproduced identically, `guided` follows the intent with each deviation justified in the
   worker's report, `free` is not checked against the mockup. The unpacked folder exists only when
   the worker was spawned on the default path, not with `--cmux`.
3. **Same-session self-review** — last resort only, when neither can run, and **say plainly
   that it is degraded** (same model, same context) rather than presenting it as equivalent to
   (1) or (2).

Whatever this session then tells the user about the review opens with the `Method:` line,
relayed verbatim before any findings — naming who answered in the full
`provider/model · 🟦🟪🟧 tier` form, and stating why `adversarial` did not answer when the
`Method:` is `reviewer` on a change that has a diff. It is the only trace of *how* the verdict
was reached.

**Three review rounds per card at most.** A round is one reviewer pass plus the fixes it triggers.
A reviewer reads without executing: each finding is a candidate until replayed. After the third round, a new finding is fixed in this card only when it is a regression shown by
running it — a test or a command whose output shows the change doing worse than the base branch.
Every other finding is written as an accepted limit in the card's `## Decisions`, and the card
closes — no follow-up card unless the user asks for one.

**A heuristic gets its differential test before the first round.** When the card changes a
heuristic over open-ended input — a parser of shell text, a pattern guard, a classifier — the
worker builds, before any review, a differential test: generated inputs run through the base
branch and the change, every changed verdict listed. The fixes land from that list in one batch;
the review then reads the differential's output along with the diff.

### The review belongs in the `/goal` condition, not in an after-the-fact hope

When a card's work needs judgment review, the review goes **into the `/goal` condition itself**, set
when the card is taken (`chantier` SKILL.md step 7): the Stop hook then holds the card open until the
transcript shows the review ran. Phrase it as a transcript-verifiable state — append to the
`## Done when`-derived condition the review clause of `chantier` SKILL.md step 7 (re-review until a
round triggers no fix, three rounds at most; each last-round finding fixed, refuted by a quoted
measurement or written as an accepted limit in the card). The clause carries the substance only — that the review ran and who
answered. The checkbox and its shape stay out of it: a form clause over a card reviewed in several
passes has no wording that answers every reading, so the Stop hook rejects work that is done.

The judgment/mechanical call is made at intake, before the diff exists — so **when it is unclear,
include the clause**: a spurious clause costs one `- [x] reviewed (n/a — mechanical)` stamp. An
*obviously* mechanical card (doc typo, rename, one-line config) carries no clause and is closed
`- [x] reviewed (n/a — mechanical)`.

`stop_handoff.py --list-unreviewed-done` (surfaced by `/chantier`'s intake) is the retroactive
net: `4-done` cards missing a checked `reviewed (...)` line, tagged `(no review line)` or
`(reviewed unchecked)`, most recent first, plus a count of cards closed `(n/a — mechanical)`. It
reaches `4-done` only — a card folded to `_ARCHIVE/` leaves the net. Not a gate — a card is never
blocked from `4-done`.

### Making the review visible on the card

When a card's work *did* need real judgment reviewed, make it visible on the card: immediately above
the `## Done when` line, add `- [ ] reviewed (method)`, `method` the review's own `Method:` line
(`adversarial`, or `reviewer` when the agent reviewed by itself) or `self-degraded` — or
`n/a — mechanical` for a mechanical card. Several passes
still make one line: the final pass first, then the earlier ones, each `reviewer` pass on a diff
followed by why `adversarial` did not answer — e.g. `- [x] reviewed (reviewer (anthropic/sonnet ·
🟪 sonnet), adversarial timed out; adversarial (<provider/model · tier>) ×2)`.
Check it — `- [x] reviewed (…)` — once the review above is done, before declaring the card done.

Added by the session doing the work, never by `/chantier`'s intake (step 7). This checkbox is
descriptive, not a gate: the review stays mandatory whenever real judgment is involved, and no script
blocks completion on a missing box. An omitted checkbox on a `4-done` card is surfaced by
`--list-unreviewed-done`; the fix is one line (`reviewed (<method>)` or `reviewed (n/a —
mechanical)`), not a re-review.

### Proposing the next card, once the card is done

Once the `## Done when` state is verified, propose candidate cards for `2-selected-for-dev`; never move
one there without an explicit OK in the same exchange — the commitment column stays a human
decision (the `kanban-proactivity` rule).

**Candidates** — open cards in the same chantier's `1-todo/` (a card still in `0-ideas/` needs
scoping first, never proposed directly), filtered and ordered:

1. **Exclude** any card whose `blocked_by` points at a card not yet `done` — never propose a
   blocked card, however old. A card missing `date:` is malformed; flag it instead of guessing a
   sort position. **Exception**: a `blocked_by` pointing at a card now `dropped` (§ below) is never
   excluded this way — it goes through the obsolescence check instead.
2. **Sort the rest into two buckets**: cards whose `blocked_by` was resolved in *this same session*
   first, then everything else. Within each bucket, sort by `priority` (skill `kanban` § Card
   frontmatter) descending, `date:` ascending as tie-break at equal `priority`.

**Nothing survives the filter** → say so plainly ("nothing to propose — `1-todo/` is empty or
everything is blocked") and propose nothing; do not lower the bar to fill a slot.

On explicit user OK: `mv` the chosen card into `2-selected-for-dev`. On silence or "not now": do
nothing, and do not repeat the proposal unprompted.

### Flagging cards the closed card just made obsolete

List every other open card (`0-ideas/`, `1-todo/`, `2-selected-for-dev/`) **in the same chantier**
with the opening of its goal, not just its title:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/kanban/scripts/open_cards_brief.py" <chantier-dir>
```

Read the just-closed card next to that listing and judge whether any is now clearly superseded. Two
triggers, same proposal:

- **Direct supersession** — the just-closed card's own content makes another open card's need moot.
- **Dead blocker** — a card `blocked_by` the just-closed card, where the closed card
  was itself `dropped` rather than finished `done`, is not silently unblocked and not left blocked
  forever: name it explicitly, next to the reason (its blocker is dead), same proposal turn as the
  next-card candidate above.

Name each candidate plainly ("`<slug>` looks superseded by what was just done — drop it?"); never
`mv`/drop without an explicit OK, same rule as the next-card proposal. On OK, drop it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/card-ops/pc_kanban_card_ops.py" drop <card-slug> <reason...>
```

Lands the card in `_ARCHIVE/CHANTIER{NNNN}-dropped/`, never `-done/` (that name presupposes
finished work) — refuses a `4-done/` source, any other column moves, a reason is mandatory and
gets appended to the card's own body before the move. A `sert` chain whose parent is
now found `dropped` there degrades exactly like a parent "not found" (`chantier/SKILL.md` step 7):
say so plainly, append no `auto-parent` block, never invent one.

Record the check on the closed card, next to its `reviewed` line:
`- [x] obsolescence (none)`, or `- [x] obsolescence (<slug>, …)` naming the cards proposed for a
drop.

**Nothing looks obsolete** → say nothing extra in the conversation; the `obsolescence (none)` line on
the card is the only trace.

### Archiving a parent whose children are still open

`move-card archive` refuses a card that an open card (`0-ideas/` to `3-in-progress/` of the same
chantier) still names in `sert` or `blocked_by`, and lists those cards. A parent with open children
(enfants ouverts) waits in `4-done/`: its children stay unblocked (`blocked_by` reads `4-done/` as
done) and its `## Requirements` rows and open questions stay defined and are printed by `story_view`
under "Requirements (open cards and 4-done)" and "Open questions"; its "Held parents" section only
lists `parent <- holders`. It is archived once its children are closed. `--force` archives it anyway —
then move any row or id an open card still cites into that card first, since a row leaves with the
archive.

### Recommending session vs spawn, next to the proposal

**Worker sessions only** — an orchestrator never takes a card itself, so it never reaches this
step. Alongside the proposed card, run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/kanban/scripts/next_card_recommendation.py"
```

It prints `{"context_window_pct": <int|null>, "rate_limit": {"five_hour": {...}, "seven_day":
{...}}|null}`, each field independently `null` when its own data source is unavailable (no state file —
`pc-statusline` not installed or not rendering —, or a context value last written more than 300 s
ago) — never an error. Both come from the state file `pc-statusline` writes on every render. Render
the line from this output and the candidate card's `repos:`/`sert` against the current session's own:

1. **Rate-limit override, checked first.** If a window in `rate_limit` has `triggered: true`,
   the whole line becomes a pause recommendation, naming the window and its `resets_at` — skip
   step 2 entirely. Only `five_hour` can trigger: at `≥95` % used with a reset more than 1h away.
   `seven_day` never triggers. Do not recommend "continue in session" when the account is about to
   get cut off regardless of context headroom.
2. **Otherwise, context-window + proximity.** `context_window_pct` — the status line's own
   `context_window.used_percentage` (`null` → omit the line entirely, nothing to base it on) against the `<40` / `≥40` band gives the default recommendation
   (continue / spawn a fresh session). The command prints that default line; when relaying it, the
   agent may add the nuance that a candidate sharing `repos:` or `sert` with the current session
   leans toward "continue", but never flips a `≥40` verdict to the opposite — this stays a
   recommendation, never a gate (the `kanban-proactivity` rule).

```
→ Proposition : <card-slug>
  💡 Reco : continuer en session (context à 22%, même repos:)
```
```
→ Proposition : <card-slug>
  ⚠️ Reco : attendre le reset (dans 1h15, five_hour à 96%) avant de démarrer — risque de hit
  mi-travail, cache froid à la reprise
```

Purely consultative: this step never `mv`s the card, never spawns a session, never forces a pause.

### Naming the session, at the end of the session only

Never per card, and not when the context is unknown (`context_window_pct` is `null`). `/rename` is
proposed only when the session ends, in one of two cases:

- **A closing at `≥40` % context** — `archive` and `drop` print the step themselves at that
  threshold.
- **The user ends the session** (says they stop, or asks for a handoff) — propose it then,
  whatever the context.

**Any session, worker or orchestrator.** The name comes from the cards **this session** has closed,
read from its own recent turn history:

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

Add nothing past it.

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
- **Work state travels in the card's handoff block, never in the message.** The handoff on the
  `3-in-progress` card is the single source of what is done and what is left; the inter-session
  message carries only the trigger.
- The receiving worker runs `/pc-kanban:chantier`, finds the card already in `3-in-progress`
  (resume-directly branch), reads the handoff, and continues.

### Abandoning a multi-repo card

If a multi-repo card is dropped mid-flight (superseded, blocked, wrong approach):

1. Close every spawned worker's cmux workspace — a `new-workspace` with nothing running in it is not
   auto-reaped, and an orphan named `<repo>-<slug>` misleads the next `ListAgents`.
2. `mv` the card back to the column it came from (`2-selected-for-dev` or `1-todo`).
3. Note the abandonment and why in the card's handoff block.
