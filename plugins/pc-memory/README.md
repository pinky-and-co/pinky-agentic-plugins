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
  closing reminder says where to promote it (a skipped procedure step: fix the skill or hook; a
  rule or pitfall beyond the directory: the source that generates the instructions, never a
  generated copy; a local trap stays in memory) or to delete it. The last line is always a tip
  (a light-bulb emoji, then: use `/memory` to enable or disable auto memory and to browse the memory
  entries of the current level), with or without stale notes. Not shown again after
  `/compact`.
- **To the model** (`additionalContext`): the index of the **parent** levels only, capped per
  level and fenced as data, not instructions. One sentence after the data notice tells the model to
  choose the level of a note by subject (a note belongs at the lowest level where it stays true for
  every session; each level's memory directory is given below), so its first path already aims at the
  right level. The sentence is absent when there is no parent context. The current level's index is
  already loaded by Claude Code.

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

### Write guard

A `PreToolUse` hook (`Write|Edit|MultiEdit|Bash`) asks the user before anything is written into a
memory directory. A note written to memory reads as a suggestion, a step of a skill is a point of
passage: told it skipped a step, Claude tends to write a `feedback_*` note instead of fixing the
skill. The hook returns `permissionDecision: ask`; the user decides each note and nothing is
blocked. It also runs inside subagents.

The confirmation dialog does not display the decision's `permissionDecisionReason` (measured in a
real session), so the reason is one short sentence and the reminder travels elsewhere: the same
output carries it to Claude as `additionalContext`. The reminder says where a note belongs: a
skipped step of a procedure is a fix to the skill or hook that owns the step; a rule or pitfall
that should apply beyond the directory goes to the source that produces the instructions (never the
generated or distributed copy, which gets overwritten); a trap that only concerns the directory is a
note.

A fourth point tells Claude at which level to write the note, since Claude chooses and the user sees
the full path in the confirmation and accepts or refuses. It lists the memory levels: the current
level (always, even without an index), then each ancestor level up to the home directory, excluded,
that has a `MEMORY.md`, each memory directory once, with paths written `~/...` (absolute outside the
home directory). It says which level the note is headed to (the level whose memory directory holds
the written path; the sentence is left out when no level does), to choose by subject, a note
belonging at the lowest level where it stays true for every session, and, when a parent is listed,
to write in that level's directory when the subject concerns it. The text is plain ASCII with no
control character. If the list cannot be built, the output keeps the `ask` and the first three
points.

The `MEMORY.md` index at the root of a memory directory is exempt: it holds the pointer line of a
note already confirmed, so one note costs one confirmation, not two. A note written straight into
the index is therefore not intercepted; a `MEMORY.md` in a sub-folder of a memory directory still
asks.

A memory directory is resolved as above (declared `autoMemoryDirectory`, else the default silo) for
the working directory's level and each ancestor level, whether or not it exists yet, plus any
default silo of any project under the config directory. `Write`, `Edit`, `MultiEdit` are matched on `file_path` (a relative path resolves from the working
directory). A `Bash` command is split into simple commands at `;`, `&&`, `||`, `|`, `&`, `(` and
newlines, and each is checked in command position:

- a redirection (`>`, `>>`, `>|`, `&>`) whose target is in memory; `/dev/null` and descriptor
  redirections (`2>&1`, `&>/dev/null`) never count, and a `<` source is not a target;
- `tee`, `rm`, `touch`, `truncate`, `sed -i` / `--in-place`, `perl -i` with a memory path argument
  (`touch -r REF` and `truncate -r REF` take REF as a source);
- `cp`, `mv`, `ln`, `install`, `rsync` with a memory path as the last argument, the destination, or
  as the `-t` / `--target-directory` value;
- `dd of=`, `curl -o` / `--output`, `wget -O` / `--output-document`, `wget -P` /
  `--directory-prefix` with a memory path.

The command is read by a small word splitter: heredoc bodies (a `<<<` here-string is not one),
text in quotes, `#` comments and arithmetic (`$((1<<n))`) are never looked into, so a commit message that quotes a memory path or a redirection asks nothing. A `$(...)` inside double quotes is read as commands.
`\rm`, `'rm'`, `sudo -u me rm` and `env -i rm` are seen as `rm`. Paths are matched after expanding
`~`, `$HOME`, `${HOME}`, `..` and symlinks; a relative path resolves from the working directory. A file
named `install.md` asks nothing.

The `Bash` net is shallow, and a false alarm costs more than a miss. Not covered:

- a script, an interpreter one-liner, `bash -c`, `sh -c`, `eval`, `find`, `xargs`, a path built at run time, a path cut by a `cd`, a
  glob, `${CLAUDE_CONFIG_DIR:-...}`, a path word that cannot be resolved (it is skipped, the rest of the command is still read);
- `mv <memory>/a.md /tmp/` removes a note without asking: only the destination is checked;
- `unlink`, `shred`, `rmdir`, `find -delete`, `git restore`, `git checkout --` and `git mv`;
- a case-insensitive file system (macOS): a path spelled with another case is not matched;
- `NotebookEdit` is not intercepted.

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
| `PC_ALLOW_MEMORY_WRITE` | unset | `1` lets memory writes through without asking; set it when launching the session |

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
