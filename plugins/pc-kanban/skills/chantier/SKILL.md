---
name: chantier
description: Resume a chantier — load its story, cards and in-progress handoff from the kanban boards visible to this session, then report state and take the next card. Use when the user says "resume chantier NNNN", or names a chantier id/slug.
argument-hint: "<chantier-id-or-slug-substring> [free-text instruction]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/story_view.py:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/activate_plugins_for_repos.py:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/pc_kanban_pending_user_action.py:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/kanban/scripts/next_card_recommendation.py:*), Bash(ls:*), Bash(rtk ls:*), Bash(find:*), Bash(rtk find:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/permission-change/scripts/guard.py:*), Bash(bash ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/resolve_repos.sh:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/hooks/card-ops/pc_kanban_card_ops.py:*), Bash(mv:*), Read, Edit, AskUserQuestion
---

# Chantier

Loads a chantier's story, live cards and in-progress handoff from every `_KANBAN/` visible to this
session, reports state, and takes the next card when the commitment column says so. Mechanics
(frontmatter schema, `model:`, `serves`, `blocked_by`): skill `kanban`.

## Arguments

First token = the chantier id (`0001`) or a slug substring; the rest is a free-text instruction
(`chantier 0001 take the card X`). Absent → list all chantiers from every board and ask which. Ids
are unique across boards; on a collision, say so and ask.

## Auto context

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py" --list-chantiers
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py" --list-agent-logs
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py" --list-unreviewed-done
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/stop_handoff.py" --list-stale
```

## Steps

1. **Resolve the chantier.** Match the first token against `CHANTIER{NNNN}-<slug>/`. Ambiguous or
  absent → `AskUserQuestion` with the candidates, then stop.
2. **List its cards.** `ls` the column dirs (`0-ideas/ 1-todo/ 2-selected-for-dev/ 3-in-progress/
  4-done/`) of this chantier only. A missing `2-selected-for-dev/` is normal.
3. **Read the story** (the spec file at the chantier root), then the open cards' requirements, open
  questions and blockers-first order:
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/story_view.py" <chantier-dir>
  ```
  A `Warning: cited but defined in no open or 4-done card` or `Warning: defined with different rows`
  line gets one line in the report.
4. **Read the live state, in order:**
  - `3-in-progress/` — the thread to resume.
  - `2-selected-for-dev/` — the work order: if non-empty, its highest `priority:` card (skill
    `kanban` § Card frontmatter) is taken next; filename sort, or an order noted in the story, only
    breaks ties.
  - `1-todo/`, `0-ideas/` — queued.
  - `4-done/` — recent decisions only.
  - the cross-agent journal listing — open entries whose title suggests this chantier.
5. **Report**, under ~15 lines:
  - one-line objective (from the story)
  - what is in progress with its handoff summary, or "nothing in progress"
  - `2-selected-for-dev/` non-empty → its highest-`priority:` card (the work order)
  - queued cards grouped by `serves` (skill `kanban` § Filiation), one group per root; `serves: null`
    cards folded into "N autonomous cards"
  - `--list-unreviewed-done` entries of **this chantier**: the count and the newest one or two, not
    the whole output. Not blocking; the fix is a `reviewed (…)` line on each (`n/a — mechanical` if
    nothing to review).
  - `--list-stale` entries of **this chantier**: the count and the most idle one or two. Not
    blocking, never a reason to re-triage (the net behind `finishing-a-card.md` § "Flagging cards
    the closed card just made obsolete").
  - a `Held parents (in 4-done, kept by open cards)` section from `story_view`: relay it — each
    parent stays in `4-done/` until the cards listed after it close (`--force` is the exit).
  - `4-done/` non-empty: flag it and offer to archive, naming any card that carries a durable
    decision (a rejected alternative written nowhere else) as an ADR candidate — never fold it into
    the story. Once the ADR candidates are settled:
```bash
    python3 "${CLAUDE_PLUGIN_ROOT}/hooks/card-ops/pc_kanban_card_ops.py" archive <card-slug>
```
    A reviewed card with no ADR candidate can skip `4-done/`: the command archives straight from
    `3-in-progress/` and refuses an unchecked `reviewed` box.
6. **Resolve `model:`, then pick the next move.**
  - Missing `model:` on a selected/in-progress card → run the classifier on it
    (`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/card-routing/pc_kanban_card_routing.py" <card>`); never pick one.
  - Card `model:` stronger than the session's → call `advisor` once at framing, before writing
    anything, and say so ("card = `<model>`, session = `<current>` → advisor()"). Never ask the user
    to switch models; propose `/model` only if they ask for a session-wide change.
  - Session at or above the card's `model:` → proceed.

  `/chantier` sets scope and goal, never the execution strategy. In order: an existing
  `context: fork` skill covering the shape (`apply-pattern` to replay a witness diff, `commit-and-pr`
  for commits) → use it; none → the session does the work itself, once. A new named agent
  (`plugins/pc-core/agents/<axis>.md`, `model:` in its frontmatter) only if the shape recurs.
  `Agent(subagent_type: "fork")` never floors a model — never use it to "run something on haiku".
7. **Resolve role, then take the card, if one is being taken.** Already in `3-in-progress/` →
  resume it; else the highest-`priority:` card of `2-selected-for-dev/` → move it into
  `3-in-progress/` and set its `date` to today. A card selected during this session is taken here,
  in place: never relaunch this skill; only the `/reload-plugins` case below asks for a new turn.
   - **Role.**
    ```bash
    git -C "${CLAUDE_PROJECT_DIR}" rev-parse --show-toplevel
    ```
    Exit 0 → **worker** (scoped to a repo). Non-zero → **orchestrator** (outside any git repo:
    board curation, selection, spawning; never edits code).
     - **Orchestrator + card `repos:` non-empty** → do not take it (no `mv`, no freeze, no Grill
       gate, no Goal): invoke `pc-kanban:spawn-workers <chantier-id> <card-slug>` and stop. The
       worker it opens runs this skill from inside the repo and does the take (`mv` + `date:` bump).
     - **Orchestrator + `repos: []`/absent**, or **worker** → continue below.
   - **Parent context** (only when moving the card into `3-in-progress/`). If `serves` is non-null,
    find the parent `<serves>.md` in this chantier's `4-done/`, else in
    `_ARCHIVE/CHANTIER<this-chantier's-number>-done/YYYY-MM-DD_<serves>.md`, or `…-dropped/` if it was
    dropped. Never glob other chantiers' archives.
    - Parent `done` with `## Done when` and `## Why` → append to the child's body (never hand-edit
      between the markers later):
      ```
      <!-- auto-parent:start -->
      ## Context inherited from `<serves>`
      <the parent's `## Done when` section, verbatim>
      <the parent's `## Why` section, verbatim>
      <!-- auto-parent:end -->
      ```
    - Parent without `## Why` → same block, `## Done when` only.
    - Parent not `done`, dropped, missing, or `serves` orphaned → say so, append nothing, never invent
      parent content.
   - **Repo-scope.** `repos: []`/absent → kanban-only work, no freeze. Otherwise resolve each
    `org/repo` slug to its local path:
    ```bash
    bash "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/resolve_repos.sh" <workspace-root> <slug...>
    ```
    0 or >1 clones for a slug (stderr, nonzero exit) → stop and say so, never guess. Then activate
    the plugins those repos need, **before** freezing:
    ```bash
    python3 "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/activate_plugins_for_repos.py" --session-id "${CLAUDE_SESSION_ID}" <resolved-repo-paths...>
    ```
    Exit 2 (session unknown) → nothing enabled: say so in one line, continue to the freeze.
    Non-empty output (one plugin id per line) = newly enabled; its skills/LSP/MCP need
    `/reload-plugins`, which step 8 depends on. Stop: no freeze, no Grill gate. Emit only:
    ```
    🔌 Next step: reload plugins before this card's work can use them.
    /reload-plugins
    ```
    Next turn the card is already in `3-in-progress/`, lands back here with nothing left to enable,
    and falls through to the freeze. Empty output → continue. A worker already scoped to the card's
    repo always gets empty output.

    Then freeze to the resolved paths **plus every kanban board root**
    (`stop_handoff.py --list-kanbans`), so the session can still maintain cards and handoffs:
    ```bash
    python3 "${CLAUDE_PLUGIN_ROOT}/skills/permission-change/scripts/guard.py" freeze <resolved-repo-paths...> <kanban-board-roots...>
    ```
    Exit 1 = the guard's project root is unknown (`SessionStart` did not export it): say so in one
    line and stop before the Grill gate; `/clear` re-runs `SessionStart`. Never freeze under another
    root. Any other non-zero exit: report it as is and stop. A repo the card does not list is never
    written across the freeze (`finishing-a-card.md` § "Touching a repo the card does not list").
   - **Grill gate — before the goal.** Set the goal only if the card is execution-ready: a concrete
    demonstrable `## Done when`, settled requirements. Any of these means it is not — a
    `## Options to explore` / `## To decide` section, an `## Open questions` entry the user could
    answer now (one only implementation can settle does not count), a "grill before coding" note,
    `requirements: []` on a non-mechanical card, a written *cause* the plan rests on
    (`verify-diagnosis`) — then emit **no** goal block: end by routing to `pc-kanban:grill`, say the
    goal is set from the rewritten `## Done when` once it converges, skip the rest of this bullet.
   - **Questions before the goal** (after the gate passes or the grill converges). Every question
    left to the user (branch, choice between designs, fallback, how a check is demonstrated) goes in
    one `AskUserQuestion` round **before** the goal block; the answers go into `<condition>`. No open
    question → skip the round; the goal block still comes last.
   - **Goal.** `<condition>` = the card's `## Done when` line; with `serves: <parent>`, walk up to the
    root card (`serves: null`) and use **its** line — the session may work any card of that group. No
    resolvable root (orphan, cycle) → say so, no `/goal` block, skip the rest of this bullet.

    `/goal` is parsed by the CLI before the turn and cannot run from a skill: the user types it.
    `<condition>` is one measurable end state, the check that proves it and the constraints that
    matter, phrased so the transcript can demonstrate it: the evaluator reads only the transcript.
    Bound it with `or stop after <N> turns` (20 unless the card says otherwise).

    **Review clause.** If the work needs judgment reviewed (anything beyond a purely mechanical
    change, per `finishing-a-card.md` § "Reviewing before declaring a card done"), append `… and
    the production-code diff has been reviewed in a fresh context, never by this session itself;
    documentation, docstrings, comments, tests and edits applying a reviewer finding as written
    never reopen a review; each finding fixed, refuted in the transcript, or noted in the card's
    `## Decisions``. Which tool reviews, re-review rounds and
    the `Method:` line stay in `finishing-a-card.md`, never in the condition. A finding on the card's subject that its
    requirements miss gets a requirement in this card and is worked here — never a new card. Substance only — never the `reviewed (…)` box. Decided pre-work from
    `## Done when` and `requirements:`; unclear → include it. Obviously mechanical (doc typo,
    rename, one-line config) → no clause, closed with `- [x] reviewed (n/a — mechanical)`.

    **Waiver clause.** Always append, before the turn bound: `… OR a clause of this condition was
    shown false by construction, the evidence quoted in the transcript, and the user, asked through
    `AskUserQuestion`, waived that clause or amended it — the amended clause then holding`. The
    evaluator honours it only for a clause that cannot hold (a tool that answers empty, a check
    that cannot run here), never for one the agent could still meet (a re-review, a commit split).

    **Amending the condition.** The evaluator judges the stored condition alone; only the user
    typing a new `/goal` changes it, and every yield before that re-fires the Stop hook.
    - A clause false by construction → one `AskUserQuestion`: the evidence quoted, the exact text of
      the amended clause in the question, only user decisions as options (waive / amend with this
      text). The answer meets the Waiver clause; no line to paste.
    - A clause the agent could meet → meet it. A clause is user-only only once tried: a typed slash
      command with local, reversible effects runs in a throwaway `claude --model haiku` session the
      agent drives in `tmux` (`send-keys`, then `capture-pane`) — the flag, never `/model`, which
      saves the user's default; an outward-facing one (push, PR, publish) stays
      a user decision through `AskUserQuestion`. Where a plugin component runs from is read from the
      repo's AGENTS.md table, never from `installPath`.
    - A doable clause the user decides to drop → the user types the new `/goal`; offer that line
      once, inside the `AskUserQuestion` that collects the decision. Never propose `/goal clear`:
      the evaluator reads it as abandoning the goal.

    **Delivery.** The CLI folds a paste over 800 characters into `[Pasted text #N]`, which `/goal`
    does not take as its condition. Pipe the finished condition (clauses and turn bound included)
    through the splitter:
    ```bash
    python3 "${CLAUDE_SKILL_DIR}/scripts/split_goal.py" <<'EOF'
    <condition>
    EOF
    ```
    Exit 1 (more than 5 blocks) → shorten the condition and rerun; exit 2 → stop and report it.
    Close the response with the script's output relayed verbatim — the `📋 Paste i/n` blocks,
    not one character changed — then, as plain text, nothing past it:
    ```
    🎯 Next step: activate the goal for this card — type `/goal `, paste each block in order, then Enter.
    ```
8. Execute the card's work. If the grill gate routed to `pc-kanban:grill`, run it first, then
  set the goal (step 7 **Goal**) from the converged `## Done when`. A worker session delegates real
  code changes to `Agent(subagent_type: "pc-kanban:card-worker")`, then reviews, fixes findings and
  closes the loop; it writes no implementation itself, except the single-mechanical-fix carve-out
  of `finishing-a-card.md` § "Who writes the work".
  A hand-back, review report or test run that disproves a premise of the goal's `<condition>` stops
  the loop: ask through `AskUserQuestion` — amend the condition (step 7 **Amending**) or rework —
  before any further step, never once the card is closed.
  **A Stop-hook rejection.** The hook judges the transcript, not the work. A rejection naming
  missing evidence (a test not re-run, a background output not read back) is met by producing it. A
  rejection on form (a line's shape, which of several passes a sentence names) is not a defect of the
  work: never edit a card, commit or artefact only to satisfy it. The second rejection on the same
  clause stops the loop: route it through step 7 **Amending the condition**. The card leaves
  `3-in-progress/` only once the transcript meets the condition.
  **Routing a send-back.** Every message to a kept worker (review finding, failing test, user feedback)
  is routed by the nature of the rework, never by the worker's context size. Rows are read top to
  bottom, the first match wins:

  | Nature of the rework | Lane |
  |---|---|
  | Single mechanical fix (`finishing-a-card.md` carve-out) | The session, in its own turn |
  | Depends on design reasoning that worker carries | Same worker (`SendMessage`) |
  | Localised and readable in the diff | Fresh worker: a new `Agent(subagent_type: "pc-kanban:card-worker")` |

  - A fresh worker runs the card's `model:`/`effort:`; model
    or effort climb only after a fresh pass missed its rework. Its prompt is the one the script
    prints (not the `spawn-workers` skill):
    `python3 "${CLAUDE_PLUGIN_ROOT}/skills/spawn-workers/scripts/pc_kanban_spawn_workers.py"
    <chantier-id> <card-slug> --model <m> --effort <e> --arbitrage <rework>` — the `Agent` call
    takes the printed `model:`/`effort:` and prompt as printed.
  - Nature undecidable → fresh worker. A missed fresh pass goes back to the same worker or the
    session; the two-send-back cap of `finishing-a-card.md` bounds the loop.
  - A model or effort change is always a new `Agent`; `SendMessage` takes neither.
  - A resume or unblock message (no rework) goes to the same worker, which alone holds its plan.

## Rules

1. Read-only for browsing: this skill edits no card to build the report. The one exception is step
  7's `2-selected-for-dev/` → `3-in-progress/` move, its freeze and, once the card clears the grill
  gate, its goal — **worker, or orchestrator on a `repos: []`/absent card only**; an orchestrator on
  a `repos:`-scoped card routes to `spawn-workers` instead. Selecting was the user's commitment, so
  it is performed directly. Every other card move happens as work progresses.
2. Reference the story and ADRs by path; never inline their content into the report.
3. Keep the report under ~15 lines — orientation, not a recap of every card.
