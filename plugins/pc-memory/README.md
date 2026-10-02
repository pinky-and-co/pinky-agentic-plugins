# pc-memory

Memory index recall for Claude Code, as a plugin.

## Overview

A session opened in a sub-repo does not see the memory notes kept at the directory levels above
it. A `SessionStart` hook reads each level's `MEMORY.md` (read-only — it never writes to memory)
and emits:

- **To the user** (`systemMessage`): one summary line per level, current included — path, number
  of notes, number at least the stale threshold old (30 d by default) — then, under it, the stale
  notes with their age,
  oldest first, at most 10 per level followed by a `+N more` marker. Each note is listed by its
  full path with the home directory written `~/` (a path outside the home directory stays
  absolute), as plain text, so a terminal that opens file paths on click can open it. The list is built from
  the notes themselves, not from the index, so a level whose `MEMORY.md` cannot be read still
  gets its summary and stale notes. No index line is shown; a level with no stale note is its summary line alone. When at least one stale note is listed, a
  closing reminder says where to promote it (procedure to a skill, general rule to `CLAUDE.md` or a
  rule at the right level, local trap stays in memory) or to delete it. Not shown again after
  `/compact`.
- **To the model** (`additionalContext`): the index of the **parent** levels only, capped per
  level and fenced as data, not instructions. The current level's index is already loaded by
  Claude Code.

Levels are the git root (or the working directory outside a repo) and each ancestor up to the home
directory, excluded; a `.git` kept in the home directory itself is never taken as a root. From
inside a git worktree the root is the main repo's, which names the memory silo. Only levels that
have a `MEMORY.md` are shown; a memory directory shared by several levels is shown once, at the
nearest. A level that cannot be read is skipped without hiding the others.

A level's memory directory is the one `autoMemoryDirectory` points to — level
`.claude/settings.local.json`, then level `.claude/settings.json`, then the user `settings.json`
— else Claude Code's default per-project silo. When a level declares one, that directory is the
only candidate: with no `MEMORY.md` in it the level shows nothing, with no fallback to the silo. A
relative path resolves from the level (level settings) or from the config directory (user
settings). The config directory is `CLAUDE_CONFIG_DIR` when set, else `~/.claude`.

A note's age is its frontmatter `modified` field, falling back to the file modification time.

On `resume`, no `SessionStart` hook runs again and the resumed session keeps the context of its
history, parent indexes included, so the hook is wired to `startup`, `clear`, `compact` and `fork`
only.

The hook never blocks the session: any error prints `{}` and exits 0.

## Enable

```bash
pinky claude-plugin install pc-memory
```

## Settings

Environment variables, all optional:

| Variable | Default | Effect |
|---|---|---|
| `PC_MEMORY_CONTEXT` | on | `0` disables the hook |
| `PC_MEMORY_STALE_DAYS` | `30` | a note at least this many days old is reported as stale |
| `PC_MEMORY_MODEL_LINES` | `30` | index lines injected per parent level into the model |

## Limits

- The folder name of the default silo (every non-alphanumeric character becomes `-`) is an observed
  rule, not a documented one. A level whose memory exists but is not found: compare
  `ls ~/.claude/projects/` with the encoded path first.
- The hook reads the level's `.claude/settings*.json` without applying Claude Code's workspace
  trust rule: a cloned repo can point `autoMemoryDirectory` at any readable `MEMORY.md`, and its
  index is then shown to the user and, for a parent level, injected into the model. Only the
  current level and its ancestors are read, never a sibling or a descendant.
- `CLAUDE_CODE_PROJECT_DIR_NAME`, which overrides the project folder name Claude Code uses for a
  silo, is not honored: a level relying on it is not found through the default silo.
- The parent indexes join the main session's prefix, so they are paid on every request — hence the
  per-level cap.
