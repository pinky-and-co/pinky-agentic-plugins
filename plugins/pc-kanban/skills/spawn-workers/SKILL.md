---
name: spawn-workers
description: From the orchestrator session, open one cmux workspace per repo of a selected card — each a repo-scoped Claude worker booting into /pc-kanban:chantier. Use after a card is selected, to hand it to worker sessions; for a multi-repo card, pass the single next repo slug.
argument-hint: "<chantier-id> <card-slug> [repo-slug...] [--dry-run]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/spawn-workers/scripts/pc_kanban_spawn_workers.py:*), Read
model: haiku
---

# spawn-workers — a cmux workspace per repo of a card

The session is bound to its cmux workspace, which is bound to one repo (skill `kanban` § Session
write-scope). So a card is worked by **one worker session per repo it touches**, not one wide
session. This skill, run from the **orchestrator** (opened outside any git repo — board curation,
selection, spawning; never edits code), opens those workspaces.

## What it does

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/spawn-workers/scripts/pc_kanban_spawn_workers.py" <chantier-id> <card-slug> [repo-slug...]
```

1. Finds `<card-slug>.md` in `CHANTIER<chantier-id>-*` across every mounted board.
2. Reads its `repos:` frontmatter (reusing `stop_handoff.py` — no `pinky` CLI anywhere).
3. Resolves each `org/repo` slug to its local clone (`chantier/scripts/resolve_repos.sh`, bounded
   `.git` scan; 0 or >1 clones for a slug → reported, never guessed). The scan root is the board's
   own workspace root (`_KANBAN`'s parent) — derived, not configured.
4. Per repo, emits:
   ```
   cmux new-workspace --cwd <path> --name "<repo>-<slug>" \
     --command 'claude -n "<repo>-<slug>" "/pc-kanban:chantier <chantier-id> <card-slug>"'
   ```
   `--name` names the workspace (cmux sidebar); `claude -n` names the session (`ListAgents`
   discovery). No `/rename` in the worker.

Pass `--dry-run` to print the command line(s) without spawning — the way to check resolution from a
session that is not the orchestrator.

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
