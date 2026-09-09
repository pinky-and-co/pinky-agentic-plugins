---
name: chantier
description: Resume a chantier — load its story, cards and in-progress handoff from the kanban boards visible to this session, then report state and take the next card. Use when the user says "resume chantier NNNN", "reprends le chantier", or names a chantier id/slug.
argument-hint: "<chantier-id-or-slug-substring> [free-text instruction]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/activate_plugins_for_repos.py:*), Bash(ls:*), Bash(find:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/permission-change/scripts/guard.py:*), Bash(bash ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/resolve_repos.sh:*), Bash(bash ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/archive_card.sh:*), Bash(mv:*), Read, Edit, AskUserQuestion
---

# Chantier

Resumes "reprends le chantier 0001": loads the story + live cards + in-progress handoff from every
`_KANBAN/` visible to this session, reports state, and — when the commitment column says so — takes
the next card. Mechanics (frontmatter schema, `model:`, `sert`, `blocked_by`) are the skill `kanban`;
read it first if unfamiliar.

## Arguments

The FIRST token is the chantier id (e.g. `0001`) or a substring of its slug; anything after is a
free-text instruction for the session (e.g. `chantier 0001 lance la card X`). Absent → list all
chantiers from every board and ask which one. Ids are unique globally, not per board — a chantier
keeps its number when it moves between boards. If two boards ever collide on an id, say so and ask.

## Auto context

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py" --list-chantiers
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py" --list-agent-logs
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py" --list-unreviewed-done
```

## Steps

1. **Resolve the chantier.** Match the first token against the `CHANTIER{NNNN}-<slug>/` listing.
  Ambiguous or absent → `AskUserQuestion` with the candidates, then stop.
2. **List its cards.** `ls` the matched chantier's column dirs
  (`0-ideas/ 1-todo/ 2-selected-for-dev/ 3-in-progress/ 4-done/`) — lazily, only this chantier.
  Most dormant chantiers have no `2-selected-for-dev/` — absent is normal.
3. **Read the story** (the chantier's spec file at its root) — the why / scope / architecture.
4. **Read the live state, in priority order:**
  - `3-in-progress/` first — an in-progress card is the thread to resume.
  - `2-selected-for-dev/` — the work order, not a queue to browse: if non-empty, its FIRST card
    (by filename sort, or an explicit order noted in the chantier's story) is what a session takes
    next.
  - `1-todo/` and `0-ideas/` for what is queued.
  - skim `4-done/` only for recent decisions.
  - the cross-agent journal listing above — open any entry whose title suggests it touched this
    chantier.
5. **Report**, under ~15 lines:
  - one-line objective (from the story)
  - what is in progress, with its handoff summary — or "nothing in progress"
  - if `2-selected-for-dev/` is non-empty: name the first card, it's the work order
  - queued cards grouped by `sert` (skill `kanban` § Filiation) — one group per root, `sert: null`
    cards elsewhere folded into a single "N autonomous cards" count
  - if the `--list-unreviewed-done` output names any card in **this chantier**: one line — the
    count (the output is already capped and `date:`-ordered, bulk is pre-convention history so do
    not paste it whole), then the newest one or two. Not blocking; the fix is a `reviewed (…)` line
    on each, `n/a — mécanique` if there was nothing to review.
  - if `4-done/` is non-empty: flag it and offer to fold + archive. Only after the user confirms
    the rationale is folded into the story/an ADR, archive via:
```bash
    bash "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/archive_card.sh" <card-path>
```
6. **Resolve `model:`, then pick the next move.**
  - Missing `model:` on a selected/in-progress card → propose one and say why, never guess silently.
  - Card's `model:` is a stronger model than the session's current one → call the `advisor` tool
    once, at framing, before writing anything, instead of proposing a session-wide `/model`
    switch. Say plainly that the mismatch triggered the call ("card = `<model>`, session =
    `<current>` → advisor() for a second opinion"). Cadrage stays in the session (interactivity is
    the point); `advisor` adds a stronger read on the plan without taking that away —
    never ask the user to switch models for this. Only propose `/model` if the user asks for a
    session-wide change outside this arbitration.
  - Session already at or above the card's `model:` → nothing to say, proceed.

  `/chantier` sets the scope and the goal, never the execution strategy — that judgment call is
  made once work starts, not here. No generic "run this on haiku" mechanism exists on purpose:
  "one routing axis = one named agent" rules out a catch-all executor that
  would become a dumping ground. In order:
  - an existing `context: fork` skill already covers this exact shape (`apply-pattern` for
    replaying a witness diff, `commit-and-pr` for commits) → use it.
  - none does → the session does the work itself, once, at its own tier — a single mechanical
    card is not worth building infrastructure for. Only if the same shape recurs does a new named
    agent (`plugins/pc-core/agents/<axis>.md`, `model:` in its own frontmatter) become a real
    deliverable.
  `Agent(subagent_type: "fork")` never floors a model — never use it to "run
  something on haiku".
7. **Take the card, if one is being taken** (already in `3-in-progress/` → resume directly; else the
  FIRST card of `2-selected-for-dev/` → move it into `3-in-progress/`, set its `date` to today):
   - **Parent context** — part of the "move it into `3-in-progress`" branch only: a card already
    resident there takes the "resume directly" branch above and never reaches this bullet, so
    nothing here needs a runtime first-time check. If `sert` is non-null, locate the parent card
    (`<sert>.md`) in *this same chantier's* `4-done/` or, once archived, its own
    `_ARCHIVE/CHANTIER<this-chantier's-number>-done/YYYY-MM-DD_<sert>.md` — never glob other
    chantiers' archives.
    - Parent found, `done`, and carries both a `## Fini =` and a `## Pourquoi` → append a managed
      block to the child's body:
      ```
      <!-- auto-parent:start -->
      ## Contexte hérité de `<sert>`
      <the parent's `## Fini =` section, verbatim>
      <the parent's `## Pourquoi` section, verbatim>
      <!-- auto-parent:end -->
      ```
      Idempotent by construction — this bullet only ever runs once per card (see above) — never
      hand-edit between the markers on a later resume.
    - Parent found but has no `## Pourquoi` → append the block with `## Fini =` only, same markers.
    - Parent not `done` yet, not found (purged without archive), or `sert` orphaned → say so
      plainly, append nothing, and move on; never invent parent content.
    - A card ending up without this block (older cards, `sert: null`, parent unavailable) parses
      exactly as before — this is appended body content, not a frontmatter field.
   - **Repo-scope.** `repos: []` or absent → kanban-only work, do not freeze (nothing to scope to).
    Otherwise, resolve each `org/repo` slug to its local absolute path:
    ```bash
    bash "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/resolve_repos.sh" <workspace-root> <slug...>
    ```
    0 or >1 local clones for a slug is reported on stderr with a nonzero exit — stop and say so,
     never guess which clone. Then activate the plugins those resolved repos need, **before**
    freezing — `claude plugin enable -s local` writes to `settings.local.json` at the workspace
    root, which is not necessarily among the paths about to be frozen, so activating after freezing
    risks the write itself being blocked by the freeze it would need to survive:
    ```bash
    python3 "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/activate_plugins_for_repos.py" <resolved-repo-paths...>
    ```
    Non-empty output (one plugin id per line) means something was newly enabled — its skills/LSP/MCP
    aren't available until `/reload-plugins` runs, and step 8's execution depends on them. Stop here:
    no freeze yet, no Session name, no Grill gate. Emit only:
    ```
    🔌 Next step: reload plugins before this card's work can use them.
    /reload-plugins
    ```
    Re-running this chantier next turn finds the card already in `3-in-progress/` (resume-directly
    branch) and lands back on this same bullet — the newly-enabled ids are now in `enabledPlugins`,
    so this step finds nothing left to activate and falls through to freezing below. Empty output on
    the first pass → nothing to activate, continue directly.

    A worker session already scoped to the card's repo (worker/orchestrator model, skill `kanban`
    § Session write-scope) has that repo's plugins ambient, so this call finds nothing to enable —
    empty output, no `/reload-plugins`. It stays wired in because the legacy one-session-many-repos
    model still coexists during the transition, where the resolved repos are not the session's cwd
    and the activation is real.

    Then freeze to the resolved paths
    **plus every kanban board root** (`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py"
    --list-kanbans`, itself multi-board aware — this workspace has more than one), so the session
    stays able to maintain cards/handoffs while frozen:
    ```bash
    python3 "${CLAUDE_PLUGIN_ROOT}/skills/permission-change/scripts/guard.py" --root <workspace-root> freeze <resolved-repo-paths...> <kanban-board-roots...>
    ```
   - **Session name.** Either branch of this step took the card — resumed from `3-in-progress/` or
    just moved there — so the session now carries one card and should be named for it. `/rename` is
    parsed at the CLI level before the turn, exactly like `/goal` below, so this skill cannot run
    it: emit it as a pasteable line at the tail of the response, **before** the `/goal` block
    (nothing may follow that — its evaluator reads only the transcript) and before the grill
    routing:

    ```
    📛 Next step: rename this session to match the card.
    /rename <card-slug>
    ```

    where `<card-slug>` is the card's filename without `.md`. The user pastes it (no turn), then
    the `/goal` line — or the grill routing — as the next message.
   - **Grill gate — before the goal.** Set the goal only when the card is execution-ready: its
    `Fini =` is a concrete demonstrable state and its requirements are settled. **If the card still
    carries open design questions** — a `## Pistes à explorer` / `## À trancher` / `## Questions
    ouvertes` section, a "grill avant de coder" note, `exigences: []` on a card that is not a
    single mechanical change, or a written *cause* the plan rests on (`verify-diagnosis`) — do
    **not** emit the goal block. The grill can rewrite the `Fini =` out from under it, and an
    active Stop-hook goal fights a multi-turn interview: the session cannot yield while the user
    thinks between rounds, so the model loops re-answering "waiting for your input" while the hook
    re-fires (observed repeatedly). Instead, end the response by routing to `pc-kanban:grill`, and
    say the goal is set from the rewritten `Fini =` once the grill converges. The **Session name**
    bullet still applies — emit its `/rename` line just before that routing. Then skip the rest of
    this bullet.
   - **Goal.** Resolve `sert`: `sert: null` → the condition is this card's own `Fini =` line;
    `sert: <parent>` → walk up to the root card (`sert: null`) this chain ultimately serves, and use
     **its** `Fini =` line instead — the session may then work any card in that group, not just this
    one. `/goal` is parsed at the CLI level, before the turn — it cannot be invoked from inside a
    running skill's own text, so do not attempt to run it. Instead, end this skill's response with:

    ```
    🎯 Next step: activate the goal for this card.
    /goal <condition>
    ```

    where `<condition>` is the resolved `Fini =` line, phrased as a demonstrable state (a test
    result, an exit code, a real diff) — `/goal`'s evaluator reads only the transcript, never a file
    or a command it would run itself.
    **Review clause.** If the card's work needs judgment reviewed — the same criterion as
    `finishing-a-card.md` § "Reviewing before declaring `Fini =` met", i.e. anything beyond a purely
    mechanical change — append to `<condition>`: `… et le diff final a été review par `adversarial`
    ou `Agent(reviewer)`, findings traités, et la case `- [x] reviewed (<méthode>)` cochée sur la
    card`. Naming the checkbox in the clause makes satisfying the goal and stamping the box one
    action, not two — so the retroactive audit does not later flag a genuinely-reviewed card.
    The decision is made pre-work, on `Fini =` and `exigences:` alone, so **when it is unclear,
    include the clause** — a spurious clause on a card that turns out mechanical costs one
    `- [x] reviewed (n/a — mécanique)` stamp at close; a missing clause on judgment work is the
    expensive failure this card exists to stop. A card that is *obviously* mechanical (doc typo, a
    rename, a one-line config) gets no clause and is closed with `- [x] reviewed (n/a — mécanique)`.
    Add nothing after that block (the **Session name** `/rename`
    line goes just before it, never after): no "would you like me to…", no rephrasing — just the
    `/goal` line, ready to paste as the next message. If no `sert` is
    resolvable (orphan card, a cycle, no root found), say so explicitly instead of inventing a
    condition.
8. Execute the card's work — or, if step 7's grill gate routed to `pc-kanban:grill`, run that
  first, then set the goal (step 7's **Goal** bullet) from the converged `Fini =`, then execute.

## Rules

1. Read-only for browsing — this skill does not edit cards to build the report. The one exception is
  step 7's `2-selected-for-dev/` → `3-in-progress/` move plus its freeze and, *when the card clears
  the grill gate*, its goal setup: the user already committed by selecting, so this performs it
  directly. Every other card move happens as work progresses.
2. Reference the story and ADRs by path — never inline their full content into the report.
3. Keep the report under ~15 lines — orientation, not a recap of every card.
