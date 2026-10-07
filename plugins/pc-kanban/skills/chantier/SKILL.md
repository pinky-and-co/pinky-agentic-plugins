---
name: chantier
description: Resume a chantier — load its story, cards and in-progress handoff from the kanban boards visible to this session, then report state and take the next card. Use when the user says "resume chantier NNNN", "reprends le chantier", or names a chantier id/slug.
argument-hint: "<chantier-id-or-slug-substring> [free-text instruction]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/story_view.py:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/activate_plugins_for_repos.py:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/pc_kanban_pending_user_action.py:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/kanban/scripts/next_card_recommendation.py:*), Bash(ls:*), Bash(rtk ls:*), Bash(find:*), Bash(rtk find:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/permission-change/scripts/guard.py:*), Bash(bash ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/resolve_repos.sh:*), Bash(bash ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/archive_card.sh:*), Bash(bash ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/drop_card.sh:*), Bash(mv:*), Read, Edit, AskUserQuestion
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
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py" --list-stale
```

## Steps

1. **Resolve the chantier.** Match the first token against the `CHANTIER{NNNN}-<slug>/` listing.
  Ambiguous or absent → `AskUserQuestion` with the candidates, then stop.
2. **List its cards.** `ls` the matched chantier's column dirs
  (`0-ideas/ 1-todo/ 2-selected-for-dev/ 3-in-progress/ 4-done/`) — lazily, only this chantier.
  Most dormant chantiers have no `2-selected-for-dev/` — absent is normal.
3. **Read the story** (the chantier's spec file at its root) — the objective and scope — then its
  open cards' requirements, open questions and blockers-first order, rebuilt on demand:
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/story_view.py" <chantier-dir>
  ```
  A `Warning: cited but defined in no open card` or `Warning: defined with different rows` line is
  worth one line in the report.
4. **Read the live state, in priority order:**
  - `3-in-progress/` first — an in-progress card is the thread to resume.
  - `2-selected-for-dev/` — the work order, not a queue to browse: if non-empty, its highest
    `priority:` card (skill `kanban` § Card frontmatter) is what a session takes next — filename
    sort, or an explicit order noted in the chantier's story, only tie-breaks at equal `priority`.
  - `1-todo/` and `0-ideas/` for what is queued.
  - skim `4-done/` only for recent decisions.
  - the cross-agent journal listing above — open any entry whose title suggests it touched this
    chantier.
5. **Report**, under ~15 lines:
  - one-line objective (from the story)
  - what is in progress, with its handoff summary — or "nothing in progress"
  - if `2-selected-for-dev/` is non-empty: name its highest-`priority:` card, it's the work order
  - queued cards grouped by `sert` (skill `kanban` § Filiation) — one group per root, `sert: null`
    cards elsewhere folded into a single "N autonomous cards" count
  - if the `--list-unreviewed-done` output names any card in **this chantier**: one line — the
    count (the output is already capped and `date:`-ordered, bulk is pre-convention history so do
    not paste it whole), then the newest one or two. Not blocking; the fix is a `reviewed (…)` line
    on each, `n/a — mechanical` if there was nothing to review.
  - if the `--list-stale` output names any card in **this chantier**: one line — the count and the
    most idle one or two. Not blocking — a fallback net behind the closing-time
    obsolescence check (`finishing-a-card.md` § "Flagging cards the closed card just made
    obsolete"), never a reason to stop and re-triage on its own.
  - if `4-done/` is non-empty: flag it and offer to archive, naming any card that carries a
    durable decision (a rejected alternative written nowhere else) as an ADR candidate — never a
    fold into the story. Once the user has settled the ADR candidates, archive via:
```bash
    bash "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/archive_card.sh" <card-path>
```
    A reviewed card with no ADR candidate may skip `4-done/` entirely — the script archives
    straight from `3-in-progress/` (it refuses an unchecked `reviewed` box, the one net
    `--list-unreviewed-done` cannot see past).
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
  made once work starts, not here. In order:
  - an existing `context: fork` skill already covers this exact shape (`apply-pattern` for
    replaying a witness diff, `commit-and-pr` for commits) → use it.
  - none does → the session does the work itself, once, at its own tier — a single mechanical
    card is not worth building infrastructure for. Only if the same shape recurs does a new named
    agent (`plugins/pc-core/agents/<axis>.md`, `model:` in its own frontmatter) become a real
    deliverable.
  `Agent(subagent_type: "fork")` never floors a model — never use it to "run
  something on haiku".
7. **Resolve role, then take the card, if one is being taken** (already in `3-in-progress/` → resume
  directly; else the highest-`priority:` card of `2-selected-for-dev/` (step 4's pick) → move it
  into `3-in-progress/`, set its `date` to today):
  A card selected during this session (after a grill or a `select`) is taken here, in place — this
  skill is already loaded, so never relaunch it; only the `/reload-plugins` case below asks for a new turn.
   - **Role.**
    ```bash
    git -C "${CLAUDE_PROJECT_DIR}" rev-parse --show-toplevel
    ```
    Exit 0 → **worker** (the session is scoped to a repo). Non-zero → **orchestrator** (opened
    outside any git repo — board curation/selection/spawning, never edits code).
     - **Orchestrator + the card has `repos:` non-empty** → do not take it: no `mv`, no freeze, no
       **Grill gate**, no **Goal**. Invoke `pc-kanban:spawn-workers
       <chantier-id> <card-slug>` instead and stop — every bullet below this one is worker-only. The
       worker it opens runs this same skill from inside the repo, lands on the worker branch, and
       performs the actual take (its own `mv` + `date:` bump — the orchestrator never does either).
     - **Orchestrator + `repos: []` or absent** → unchanged: continue below (in-session kanban-only
       curation — nothing to scope to).
     - **Worker** → unchanged: continue below.
   - **Parent context** — part of the "move it into `3-in-progress`" branch only: a card already
    resident there takes the "resume directly" branch above and never reaches this bullet, so
    nothing here needs a runtime first-time check. If `sert` is non-null, locate the parent card
    (`<sert>.md`) in *this same chantier's* `4-done/` or, once archived, its own
    `_ARCHIVE/CHANTIER<this-chantier's-number>-done/YYYY-MM-DD_<sert>.md` — or, if the parent was
    `dropped` rather than finished, `_ARCHIVE/CHANTIER<this-chantier's-number>-dropped/
    YYYY-MM-DD_<sert>.md`. Never glob other chantiers' archives.
    - Parent found, `done`, and carries both a `## Done when` and a `## Why` → append a managed
      block to the child's body:
      ```
      <!-- auto-parent:start -->
      ## Context inherited from `<sert>`
      <the parent's `## Done when` section, verbatim>
      <the parent's `## Why` section, verbatim>
      <!-- auto-parent:end -->
      ```
      Never hand-edit between the markers on a later resume.
    - Parent found but has no `## Why` → append the block with `## Done when` only, same markers.
    - Parent not `done` yet, `dropped`, not found (purged without archive), or
      `sert` orphaned → say so plainly, append nothing, and move on; never invent parent content.
    - A card ending up without this block (older cards, `sert: null`, parent unavailable) parses
      exactly as before — this is appended body content, not a frontmatter field.
   - **Repo-scope.** `repos: []` or absent → kanban-only work, do not freeze (nothing to scope to).
    A repo the card does not list is never written across the freeze: see `finishing-a-card.md` §
    "Touching a repo the card does not list".
    Otherwise, resolve each `org/repo` slug to its local absolute path:
    ```bash
    bash "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/resolve_repos.sh" <workspace-root> <slug...>
    ```
    0 or >1 local clones for a slug is reported on stderr with a nonzero exit — stop and say so,
     never guess which clone. Then activate the plugins those resolved repos need, **before**
    freezing:
    ```bash
    python3 "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/activate_plugins_for_repos.py" --session-id "${CLAUDE_SESSION_ID}" <resolved-repo-paths...>
    ```
    Exit 2 (session unknown) → nothing is enabled: say so in one line and continue to the freeze.
    Non-empty output (one plugin id per line) means something was newly enabled — its skills/LSP/MCP
    aren't available until `/reload-plugins` runs, and step 8's execution depends on them. Stop here:
    no freeze yet, no Grill gate. Emit only:
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
    empty output, no `/reload-plugins`.

    Then freeze to the resolved paths
    **plus every kanban board root** (`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py"
    --list-kanbans`, itself multi-board aware — this workspace has more than one), so the session
    stays able to maintain cards/handoffs while frozen:
    ```bash
    python3 "${CLAUDE_PLUGIN_ROOT}/skills/permission-change/scripts/guard.py" --root <workspace-root> freeze <resolved-repo-paths...> <kanban-board-roots...>
    ```
   - **Grill gate — before the goal.** Set the goal only when the card is execution-ready: its
    `## Done when` is a concrete demonstrable state and its requirements are settled. **If the card
    still carries open design questions** — a `## Options to explore` / `## To decide`
    section, an `## Open questions` entry the user could answer now (one only implementation can
    settle does not count), a "grill before coding" note, `requirements: []` on a card that is not
    a single mechanical change, or a written *cause* the plan rests on (`verify-diagnosis`) — do
    **not** emit the goal block. Instead, end the response by routing to `pc-kanban:grill`, and
    say the goal is set from the rewritten `## Done when` once the grill converges. Then skip the
    rest of this bullet.
   - **Questions before the goal — once the Grill gate has passed or the grill has converged.** Every
    question still left to the user (a branch to work on, a choice between two designs, a fallback if a premise falls, how a
    check will be demonstrated) is asked **before** the goal block is emitted, in one
    `AskUserQuestion` round, and the answers are written into the `<condition>`. A card with no
    open question skips the round; the goal block still comes last.
   - **Goal.** Resolve `sert`: `sert: null` → the condition is this card's own `## Done when` line;
    `sert: <parent>` → walk up to the root card (`sert: null`) this chain ultimately serves, and use
     **its** `## Done when` line instead — the session may then work any card in that group, not
    just this one.

    **Orphan `sert`.** If no `sert` is resolvable (orphan card, a cycle, no root found), say so
    explicitly instead of inventing a condition — no `/goal` block, nothing past it — and skip the
    rest of this bullet.

    `/goal` is parsed at the CLI level, before the turn — it cannot be invoked from
    inside a running skill's own text, so do not attempt to run it. Instead, end this skill's
    response with:

    ```
    🎯 Next step: activate the goal for this card.
    /goal <condition>
    ```

    where `<condition>` is the resolved `## Done when` line, phrased as a demonstrable state (a test
    result, an exit code, a real diff) — `/goal`'s evaluator reads only the transcript, never a file
    or a command it would run itself.
    **Review clause.** If the card's work needs judgment reviewed — the same criterion as
    `finishing-a-card.md` § "Reviewing before declaring the card done", i.e. anything beyond a
    purely mechanical change — append to `<condition>`: `… and the final diff has been reviewed by
    `Agent(reviewer)`, findings addressed, the report's `Method:` line relayed verbatim to the
    user, its label completed to the full `provider/model · 🟦🟪🟧 tier` form from the report's
    `[answered by …]` line when the first line carries the model name alone —
    an `adversarial` call that returned nothing stated as such — verdict on the reviewer alone —
    and the `- [x] reviewed (<method>)` box checked on the card` — `<method>` is the `Method:`
    line the reviewer opens its report with. The decision is made pre-work, on `## Done when` and
    `requirements:` alone, so **when it is unclear, include the clause**. A card that
    is *obviously* mechanical (doc typo, a rename, a one-line config) gets no clause and is closed
    with `- [x] reviewed (n/a — mechanical)`.
    **Escape clause.** Append an OR branch to `<condition>`: `… OR the agent's last message names a
    precise, user-only action (a literal command to paste, a named permission to grant,
    `/reload-plugins`, an input asked for by name) that is the only remaining path — no
    agent-executable alternative is available — and the agent yielded the turn without relaunching a
    tool`.
    Anti-cheat: the named action must be precise, nameable, and necessary — never a vague "I'm
    stuck", and never an action the agent could have run itself instead of yielding.
    Once `<condition>` is fully resolved (clauses included), queue this bullet's own `goal` key —
    the last tool call of the turn (single-quote the message, escaping any literal `'` inside it as
    `'\''`):

    ```bash
    python3 "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/pc_kanban_pending_user_action.py" require goal '/goal <condition>' --surfaced
    ```

    Only after that call returns, close the response with the `/goal` block above — as plain text,
    no tool call between or after it. Add nothing past it: no "would you like me to…", no
    rephrasing — just the one pasteable line, ready to paste as the next message.
8. Execute the card's work — or, if step 7's grill gate routed to `pc-kanban:grill`, run that
  first, then set the goal (step 7's **Goal** bullet) from the converged `## Done when`, then
  execute. "Execute" means, per `finishing-a-card.md` § "Who writes the work": a worker session
  delegates real code changes to `Agent(subagent_type: "pc-kanban:card-worker")` and then reviews,
  fixes findings, and closes the loop — it does not write the implementation in its own turn,
  except for the single-mechanical-fix carve-out that section names.

## Rules

1. Read-only for browsing — this skill does not edit cards to build the report. The one exception is
  step 7's `2-selected-for-dev/` → `3-in-progress/` move plus its freeze and, *when the card clears
  the grill gate*, its goal setup — **worker branch, or orchestrator on a `repos: [] or absent`
  card only**; an orchestrator on a `repos:`-scoped card never performs this move (step 7's
  **Role** bullet routes it to `spawn-workers` instead). Where it does apply, the user already
  committed by selecting, so this performs it directly. Every other card move happens as work
  progresses.
2. Reference the story and ADRs by path — never inline their full content into the report.
3. Keep the report under ~15 lines — orientation, not a recap of every card.
