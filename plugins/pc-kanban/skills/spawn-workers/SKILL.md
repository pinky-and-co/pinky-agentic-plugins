---
name: spawn-workers
description: From the orchestrator session, hand a selected card to one worker session per repo of its `repos:` — inside cmux/wmux it opens the workspaces, outside it prints the commands to run. Each worker boots into /pc-kanban:chantier on the card's `model:`. For a multi-repo card, pass the single next repo slug.
argument-hint: "<chantier-id> <card-slug> [repo-slug...] [--dry-run]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/spawn-workers/scripts/pc_kanban_spawn_workers.py:*), Read
model: haiku
---

# spawn-workers — a worker session per repo of a card

The session is bound to its cmux/wmux workspace, which is bound to one repo (skill `kanban`
§ Session write-scope). So a card is worked by **one worker session per repo it touches**, not one
wide session. This skill, run from the **orchestrator** (opened outside any git repo — board
curation, selection, spawning; never edits code), opens those sessions.

## What it does

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/spawn-workers/scripts/pc_kanban_spawn_workers.py" <chantier-id> <card-slug> [repo-slug...]
```

1. Finds `<card-slug>.md` in `CHANTIER<chantier-id>-*` across every mounted board, and reads its
   `repos:` and `model:` — all via `stop_handoff.py`'s CLI, no `pinky` CLI anywhere.
2. Resolves each `org/repo` slug to its local clone (`chantier/scripts/resolve_repos.sh`, bounded
   `.git` scan; 0 or >1 clones for a slug → reported, never guessed). The scan root is the board's
   own workspace root (`_KANBAN`'s parent) — derived, not configured.
3. Builds the worker's `claude` invocation:
   ```
   claude --name "<repo>-<slug>" --model <card model> [--advisor <one tier up>] "/pc-kanban:chantier <id> <slug>"
   ```
   `--advisor` is `sonnet` for a `haiku` card, `opus` for a `sonnet` card, and absent for `opus` /
   `fable` (already the ceiling). `claude --name` names the session (`ListAgents` discovery); the
   cmux/wmux workspace adopts that name, so no separate workspace-name flag.
4. Then, per repo:
   - **Inside cmux / wmux** (`CMUX_SURFACE_ID` / `WMUX_SURFACE_ID` set) →
     `<mux> new-workspace --cwd <path> --command '<the claude line>'`.
   - **Outside any multiplexer** → prints `cd <path> && <the claude line>` for the user to run, and
     spawns nothing.

Pass `--dry-run` to print the command line(s) without spawning.

## Multi-repo cards — sequential by convention

Most multi-repo cards are the `pinky-agentic-plugins` + `pinky-bootstrap` pair, where the flow is
sequential (bootstrap is the vendored source, plugins the copy). Do **not** spawn every repo at
once for those:

- Orchestrator spawns only the first: `spawn-workers <id> <slug> pinky-and-co/pinky-bootstrap`.
- That worker, its part of `Fini =` reached, relays to the next — see
  `kanban/references/finishing-a-card.md` § "Relaying a multi-repo card". It re-runs this skill
  with the next slug, or `SendMessage`s a bare trigger to an already-listed worker session.

Spawn every repo in one call (no slug filter) only for a card whose repos are genuinely disjoint.

## Rules

1. Orchestrator-only. A worker session is already repo-scoped; it runs this skill only to relay to
   the *next* repo of its own card, never to spawn its own.
2. The skill spawns; it never moves the card or sets a goal — the worker's own `/pc-kanban:chantier`
   run does that (it finds the card already in `3-in-progress`).
3. Work state for a multi-repo hand-off lives in the card's handoff block, never in the
   inter-session message (`finishing-a-card.md`).
