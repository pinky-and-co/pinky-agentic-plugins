# pc-kanban

File-based kanban board mechanics — chantiers, cards, permission guards (careful/freeze/deny), requirements interviews.

## Overview

The board lives as plain files under `_KANBAN/` — no external ticketing tool, no server. A chantier (epic) is a directory of cards moving through columns, and the agent maintains the board as part of the work instead of waiting to be told "create a card" or "move it to done". The plugin ships the skills that read and write that board, the permission guards that scope a session's writes, a requirements interview that turns ideas into cards, and an agent-plus-worker path for multi-repo cards. The permission guards ride alongside the board mechanics because a mounted board needs writes an ordinary repo-scoped session shouldn't otherwise have — see the `permission-change` skill and `hooks/orchestrator_freeze.py`.

## Skills

### `brainstorm`
Diverge before deciding — explore an idea with the user, name the concepts it hides, challenge generic claims, then hand back a brief where every claim carries its epistemic status, open items are listed and implementation steps are ordered. Use when starting from a vague idea, before any design is chosen, or to prepare a brief that a later requirements interview will consume.

```
/pc-kanban:brainstorm <idea>
```

The skill also runs on claude.ai. The zip is generated on demand and never committed:

```
python3 plugins/pc-kanban/skills/brainstorm/scripts/build_zip.py
```

It prints the path of `brainstorm-skill.zip` in the system temporary directory (`--out <path>` picks another location, outside the repository). On claude.ai, open Settings, Capabilities, Skills, upload the zip, and enable the skill. Run a brainstorm there, save the brief as a file, then run `pc-kanban:grill <path-to-brief.md>` in Claude Code.

### `chantier`
Resume a chantier — load its story, cards and in-progress handoff from the kanban boards visible to this session, then report state and take the next card. The taking of a card moves it to in-progress, freezes writes to the card's `repos:`, and routes the implementation to the `card-worker` agent.

```
/pc-kanban:chantier 0007
```

### `grill`
Interview a plan, an idea or an imported brief until nothing is silently assumed, then write the result into the chantier's cards — numbered, verifiable requirements, open questions and steps, each in the card that carries it. Use before starting a chantier, to stress-test a design, or to land an external brainstorm into the board.

```
/pc-kanban:grill docs/brief.md
```

### `init`
Scaffold a fresh `_KANBAN/INDEX.md` + nested `_AGENT_LOG/` at a workspace root, declare it in `~/.claude/settings.pinky.json` (`kanban_boards`), and grant it native access in `~/.claude/settings.json` (`permissions.additionalDirectories`). Idempotent throughout.

```
/pc-kanban:init
```

### `kanban` *(reference)*
Mechanics of the file-based kanban board used to track chantiers (epics) and cards (tasks) — column lifecycle, card frontmatter schema, filiation, cross-agent log. Read when a session reads, moves or writes a `_KANBAN/CHANTIER*/**/*.md` card, or needs to resolve which repos/model/goal a card implies.

### `move-card`
Move one kanban card by its slug — `select` (1-todo to 2-selected-for-dev, checked), `archive` (done card, dated archive) or `drop` (obsolete card, with a reason). Typed with a conforming verb and slug it runs with no model call.

```
/pc-kanban:move-card select 0025/open-points-without-card
```

### `permission-change`
Toggle the PreToolUse permission guards — `careful` (confirm destructive Bash), `freeze` (lock edits to a scope), `deny` (block edits to a scope), or `guard` (careful + freeze together). `status` inspects the current state without changing it.

```
/pc-kanban:permission-change freeze src/core
```

### `spawn-workers`
From the orchestrator session, hand a selected card to one `card-worker` Agent call per repo of its `repos:` with a generated prompt (not a cmux session); `--cmux` keeps the session path for a repo whose environment doesn't reduce to commands. For a multi-repo card, pass the single next repo slug.

```
/pc-kanban:spawn-workers 0007 fix-login
```

### `yt`
Summarize a YouTube video (any language, auto-detected) and give a relevance verdict against the active chantier board — the verdict is the feature. Deterministic fetch (yt-dlp → VTT → clean text), analysis left to the LLM; `--card` files the verdict into a `0-ideas` card.

```
/yt <url>
```

## Hooks

- **`kanban_proactivity.py`** (`SessionStart`, matcher `startup|compact|clear`) — injects the kanban-proactivity rule (capture → card, select, move, handoff, narrate → log) into the main session; a subagent never fires SessionStart, so the main session only.
- **`orchestrator_freeze.py`** (`SessionStart`) — freezes an orchestrator session (cwd outside any git repo, at least one board mounted) to writes in the mounted boards' card + agent-log directories, `careful` on — derived, not declared; a session opened inside a repo is a worker and left alone.
- **`pc_kanban_guard_env_hook.py`** (`SessionStart`) — exports the session's project root to `Bash` as `CLAUDE_PROJECT_DIR`.
- **`guard.py`** (`PreToolUse`, `Bash|Edit|Write|MultiEdit|NotebookEdit`) — enforces the careful/freeze/deny/guard guards per session, one shared state file (`<project root>/.claude/.guard-state.json`, one entry per session id).
- **`card_discipline.py`** (`PreToolUse`, `Edit|Write|MultiEdit|NotebookEdit`) — asks before writing a kanban card still being framed (`0-ideas`, `1-todo`, `2-selected-for-dev`; creating one counts), and before creating a card whose slug exceeds the 30-character maximum.
- **`stop_handoff.py`** (`Stop` and `SessionEnd`) — keeps the in-progress card's managed auto-handoff block fresh (branch, uncommitted files, recent commits) whenever a turn ends with a dirty git tree. Doubles as the board- and agent-log-discovery CLI (`--list-chantiers`, `--list-kanbans`, …); other plugins' guards shell out to it rather than re-deriving board discovery.
- **`pc_kanban_guard_session_hook.py`** (`SessionEnd` and `DirectoryAdded`) — drops the session's guard state on SessionEnd, whatever its reason; records a `/add-dir` directory per session on DirectoryAdded.
- **`agent_log_writer.py`** (`SessionEnd`) — when the session committed or touched the board but wrote nothing under `_AGENT_LOG/`, hands the entry to a detached headless writer (`claude -p`, haiku, restricted to Read/Glob/Write inside `_AGENT_LOG/`) fed a scrubbed transcript excerpt; the writer may decide nothing is worth logging.
- **`pc_kanban_direct_command_runner.py`** (`UserPromptExpansion`) — runs the deterministic skill scripts of `init`, `permission-change` and `move-card` directly, with no model call, when the typed command matches its conforming grammar.
- **`board-pane`** (declared in `hooks.json` `modules`) — the `/board` pane: the kanban cards of the session's repos, browsable and actionable; every card fact comes from `board_data.py`, the module only filters, sorts and draws.

Scripts under `hooks/` that no hook entry wires but the plugin's skills and guards use: `pc_kanban_secret_patterns.py` (secret-pattern definitions shared by this plugin's guards), `card-routing/pc_kanban_card_routing.py` (runs pc-core's routing classifier to write a card's `model:`/`effort:`), `card-ops/pc_kanban_card_ops.py` (the select/archive/drop engine behind `move-card`), `requirement-rows/pc_kanban_requirement_rows.py` (parses a chantier's `## Requirements` rows, shared by the `chantier` and `spawn-workers` skills), `agent-log-migration/pc_kanban_migrate_agent_log.py` (one-time nesting of a pre-existing sibling `_AGENT_LOG/` under `_KANBAN/`).

## Requirements

- Python 3.12 or newer, resolved as `python3` on the `PATH`, for the hooks and skill scripts (stdlib only — no pip install).
- `git` — resolving a card's `repos:` slugs to local clones, the worker/orchestrator role check, and deriving `org/repo` slugs from `git remote`.
- `claude` on the `PATH` — `agent_log_writer.py` spawns `claude -p` for its headless writer.
- `yt-dlp` on the `PATH` — the `yt` skill's fetch (provisioned by the workspace setup).
- A `cmux`/`wmux` multiplexer only for `spawn-workers --cmux` inside one; outside, the command line is printed for the user to run.

## Dependencies

`pc-core` — the card-routing classifier runs pc-core's `subagent-routing/classify.py`.
