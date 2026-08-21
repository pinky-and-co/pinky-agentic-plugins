---
name: chantier
description: Resume a chantier — load its story, cards and in-progress handoff from the kanban boards visible to this session, then report state and take the next card. Use when the user says "resume chantier NNNN", "reprends le chantier", or names a chantier id/slug.
argument-hint: "<chantier-id-or-slug-substring> [free-text instruction]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py:*), Bash(ls:*), Bash(find:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/permission-change/scripts/guard.py:*), Bash(bash ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/resolve_repos.sh:*), Bash(mv:*), Read, Edit, AskUserQuestion
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
   - if `4-done/` is non-empty: flag it and offer to fold + archive
6. **Resolve `model:`, then pick the next move.**
   - Missing `model:` on a selected/in-progress card → propose one and say why, never guess silently.
   - Card's `model:` is a stronger model than the session's current one → call the `advisor` tool
     once, at framing, before writing anything, instead of proposing a session-wide `/model`
     switch. Say plainly that the mismatch triggered the call ("card = `<model>`, session =
     `<current>` → advisor() for a second opinion"). Cadrage stays in the session (interactivity is
     the point, ADR-0004); `advisor` adds a stronger read on the plan without taking that away —
     never ask the user to switch models for this. Only propose `/model` if the user asks for a
     session-wide change outside this arbitration.
   - Session already at or above the card's `model:` → nothing to say, proceed.

   `/chantier` sets the scope and the goal, never the execution strategy — that judgment call is
   made once work starts, not here. No generic "run this on haiku" mechanism exists on purpose:
   "one routing axis = one named agent" (decided 2026-08-14) rules out a catch-all executor that
   would become a dumping ground. In order:
   - an existing `context: fork` skill already covers this exact shape (`apply-pattern` for
     replaying a witness diff, `commit-and-pr` for commits) → use it.
   - none does → the session does the work itself, once, at its own tier — a single mechanical
     card is not worth building infrastructure for. Only if the same shape recurs does a new named
     agent (`plugins/pc-core/agents/<axis>.md`, `model:` in its own frontmatter) become a real
     deliverable.
   `Agent(subagent_type: "fork")` never floors a model (see ADR-0004) — never use it to "run
   something on haiku".
7. **Take the card, if one is being taken** (already in `3-in-progress/` → resume directly; else the
   FIRST card of `2-selected-for-dev/` → move it into `3-in-progress/`, set its `date` to today under
   `status: in-progress`):
   - **Repo-scope.** `repos: []` or absent → kanban-only work, do not freeze (nothing to scope to).
     Otherwise, resolve each `org/repo` slug to its local absolute path:
     ```bash
     bash "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/resolve_repos.sh" <workspace-root> <slug...>
     ```
     0 or >1 local clones for a slug is reported on stderr with a nonzero exit — stop and say so,
     never guess which clone. Then freeze to the resolved paths **plus every kanban board root**
     (`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py" --list-kanbans`, itself multi-board
     aware — this workspace has more than one), so the session stays able to maintain cards/handoffs
     while frozen:
     ```bash
     python3 "${CLAUDE_PLUGIN_ROOT}/skills/permission-change/scripts/guard.py" --root <workspace-root> freeze <resolved-repo-paths...> <kanban-board-roots...>
     ```
   - **Goal.** Resolve `sert`: `sert: null` → the condition is this card's own `Fini =` line;
     `sert: <parent>` → walk up to the root card (`sert: null`) this chain ultimately serves, and use
     **its** `Fini =` line instead — the session may then work any card in that group, not just this
     one. Set the session goal from that line by running `/goal <condition>` (native Claude Code
     feature, v2.1.139+). **Not verified as invocable from inside a skill in every harness** — if the
     attempt has no visible effect (no `◎ /goal active` indicator), say so plainly and state the goal
     in the response instead, rather than silently pretending it is active. Either way, phrase the
     condition so it is demonstrable in the transcript (a test result, an exit code, a real diff) —
     `/goal`'s evaluator reads only the transcript, never a file or a command it would run itself.
8. Execute the card's work.

## Rules

1. Read-only for browsing — this skill does not edit cards to build the report. The one exception is
   step 7's `2-selected-for-dev/` → `3-in-progress/` move plus its freeze/goal setup: the user already
   committed by selecting, so this performs it directly. Every other card move happens as work
   progresses.
2. Reference the story and ADRs by path — never inline their full content into the report.
3. Keep the report under ~15 lines — orientation, not a recap of every card.
