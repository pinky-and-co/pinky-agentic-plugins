---
name: spawn-workers
description: From the orchestrator session, hand a selected card to a `card-worker` Agent call per repo of its `repos:` — a generated prompt, not a cmux session on /pc-kanban:chantier. `--cmux` keeps the session path for a repo whose environment doesn't reduce to commands. For a multi-repo card, pass the single next repo slug.
argument-hint: "<chantier-id> <card-slug> [repo-slug...] [--dry-run] [--cmux] [--model <tier>] [--arbitrage <text>]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/spawn-workers/scripts/pc_kanban_spawn_workers.py:*), Read, Agent
model: haiku
---

# spawn-workers — a card-worker Agent call per repo of a card

A card is worked by **one worker per repo it touches**, not one wide session. This skill, run from
the **orchestrator** (opened outside any git repo — board curation, selection, spawning; never
edits code), hands each repo's slice to a `card-worker` Agent call built from a fully generated
prompt — never a hand-typed one (a 2026-09-20 witness run measured what a hand-typed prompt missed:
the target repo's own test location, retyped from memory instead of read off disk).

## Default path — an `Agent` call per repo

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/spawn-workers/scripts/pc_kanban_spawn_workers.py" <chantier-id> <card-slug> [repo-slug...] [--model <tier>] [--arbitrage <text>]
```

1. Finds `<card-slug>.md` in `CHANTIER<chantier-id>-*` across every mounted board, and reads its
   `repos:` and `model:` — all via `stop_handoff.py`'s CLI, no `pinky` CLI anywhere.
2. Resolves each `org/repo` slug to its local clone (`chantier/scripts/resolve_repos.sh`, bounded
   `.git` scan; 0 or >1 clones for a slug → reported, never guessed). The scan root is the board's
   own workspace root (`_KANBAN`'s parent) — derived, not configured.
3. For each repo, prints:
   ```
   === worker: <repo>-<slug> ===
   model: <tier>
   --- prompt ---
   <the two-block prompt>
   ```
   `<tier>` is `--model` when given, else the card's own `model:` frontmatter (`effective_model`) —
   the caller's judgment on how much the card is already briefed, never a new card field. The
   prompt (`build_worker_prompt`) is two blocks:
   - **fixed** — this repo's own conventions (`repo_conventions_block`), probed from disk/PATH:
     where its Python tests live, its test command, its gate command, and the standing worker
     invariants (dedicated branch, no commit/push/PR). The same every time for this repo — never
     retyped by whoever is running this skill.
   - **generated** — the card's absolute path, the repo, and `--arbitrage` (any judgment call the
     card leaves for the worker to resolve rather than ask back).
4. **This script never calls the `Agent` tool — only the calling session can.** For each block of
   printed output, invoke:
   ```
   Agent(subagent_type: "card-worker", model: <the printed tier>, prompt: <the printed prompt>)
   ```
   one call per repo. `card-worker`'s own agent definition (`agents/card-worker.md`) carries the
   role invariants (one card, no board file, no column move, hand-back format); the printed prompt
   carries the task and the repo facts. No `skills:` preload — the worker shells the repo's own
   test/gate commands directly.

## `--cmux` — the session path, for an environment that doesn't reduce to commands

A repo whose worker needs something a subagent cannot get cold — a project-scoped plugin resolved
at process start, a scoped MCP server — still needs a real `claude` process, not an `Agent` call.
Pass `--cmux` for that repo; nothing here decides it automatically, because the criterion is the
repo's own environment, not the card's size:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/spawn-workers/scripts/pc_kanban_spawn_workers.py" --cmux <chantier-id> <card-slug> [repo-slug...]
```

This is the previous default, unchanged: builds `claude --name "<repo>-<slug>" --model <tier>
--advisor <one tier up> "/pc-kanban:chantier <id> <slug>"` (tier verbatim, its `[1m]` long-context
variant where one exists; `--advisor` one tier up, absent for `opus`/`fable`), enables — at project
scope, before opening the worker — every installed project-scoped plugin whose `projectPath` covers
the repo (`matching_plugin_ids`; each enable bounded, tried once, a failure logged and skipped), then:

- **Inside cmux / wmux** (`CMUX_SURFACE_ID` / `WMUX_SURFACE_ID` set) →
  `<mux> new-workspace --cwd <path> --command '<the claude line>'`.
- **Outside any multiplexer** → prints `cd <path> && <the claude line>` for the user to run, and
  spawns nothing.

`--dry-run` only affects `--cmux`: it previews the command line(s) and skips the project-scope
plugin enable. The default path already does nothing but print, so `--dry-run` has nothing to add
there.

## Multi-repo cards — sequential by convention

Most multi-repo cards are the `pinky-agentic-plugins` + `pinky-bootstrap` pair, where the flow is
sequential (bootstrap is the vendored source, plugins the copy). Do **not** spawn every repo at
once for those:

- Orchestrator spawns only the first: `spawn-workers <id> <slug> pinky-and-co/pinky-bootstrap`.
- That worker, its part of `## Done when` reached, relays to the next — see
  `kanban/references/finishing-a-card.md` § "Relaying a multi-repo card". It re-runs this skill
  with the next slug, or `SendMessage`s a bare trigger to an already-listed worker session.

Spawn every repo in one call (no slug filter) only for a card whose repos are genuinely disjoint.

## Rules

1. Orchestrator-only. A worker session is already repo-scoped; it runs this skill only to relay to
   the *next* repo of its own card, never to spawn its own.
2. The skill itself never moves the card. In `--cmux` mode, the worker's own `/pc-kanban:chantier`
   run does that (its `chantier/SKILL.md` step 7 moves the card from `2-selected-for-dev` to
   `3-in-progress` and bumps `date:`). A `card-worker` Agent call never touches the board at all
   (its own agent definition forbids it) — the orchestrator is the one that moves the card, since
   it is the only actor in that flow ever allowed to.
3. Work state for a multi-repo hand-off lives in the card's handoff block, never in the
   inter-session message (`finishing-a-card.md`).
