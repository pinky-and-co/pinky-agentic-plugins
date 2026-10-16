---
name: permission-change
description: Toggle the PreToolUse permission guards — careful (confirm destructive Bash), freeze (lock edits to a scope), deny (block edits to a scope), or guard (careful + freeze together). Use when the user asks to change or check a permission guard.
argument-hint: "careful|freeze|guard|deny [scope-dir...] [on|off] | status"
arguments: [mode]
model: haiku
allowed-tools: Bash(python3:*), Bash(py:*)
---

# /permission-change — one entry point for the permission guards

Four related guards, one command — `$mode` (the first word) picks which. All four are enforced by
the same PreToolUse hook (`guard.py`) and share one state file; `$mode` decides what gets written
to it.

**Why `arguments: [mode]` and not `[mode, scope]` or more**: `$mode` is always the first token,
unambiguous whatever follows. Everything after it is variadic and mode-dependent (0+ scope
directories for `freeze`/`deny`/`guard`, or a bare `on`/`off` for `careful`) — a fixed named slot
for it would mislabel real invocations, e.g. `/permission-change freeze src/core tests` has TWO
scope directories; a `$scope` bound to position 1 would silently drop `tests` and a `$status`
bound to position 2 would call it a status when it is really the second directory. `$ARGUMENTS`
(the full string) stays the only correct vehicle for that variadic remainder — see Action below.

## Modes

- **`careful [on|off]`** — while on, a destructive `Bash` command (`rm -rf`,
  `git push --force`, `git reset --hard`, `DROP`, `TRUNCATE`, `DELETE FROM` without `WHERE`,
  `sudo`, …) returns `permissionDecision: ask` instead of running silently. A speed bump, not
  a block.
- **`freeze <dir>... | off`** — a whitelist: locks `Edit`/`Write`/`MultiEdit`/
  `NotebookEdit` to the given directories; a target inside the project root or an added
  directory but outside them is denied. A target further out reaches the always-on outside-scope
  guard first and gets its `ask`, never a `deny`.
- **`deny <dir>... | off`** — the inverse of `freeze`: a blocklist. The given
  directories become unwritable; everything else stays open. Not the same as the
  `permissions.deny` settings key (harder guarantee, session-start only, not a toggle).
- **`guard <dir>... | on | off`** — convenience: `careful` on + `freeze` to the given
  directories in one call.

**What `freeze` asks for in `Bash`, and its honest limit.** `careful` and `deny` cover their own
scope only (`careful`: destructive `Bash`; `deny`: the write-class tools). `freeze` also asks for a
`Bash` command that looks like a write (`>`, `tee`, `sed -i`, `mv`/`cp`/`rm`, `git commit`…) and
names an absolute or `~` path, a `cd` target or a `git -C` directory outside the frozen scope. It
is a string heuristic. Four rules keep text from counting as a path:

- **Only the target of a write is judged.** The command is cut at `;`, `&` and `|`; a segment
  with no write word is not read, and in one that has it only the paths from that word on count.
  The program, its script and its read-only arguments are never a target, even when another
  segment of the line writes (`mv a b; bash <plugin script> <dir>`). The first absolute path of a
  `cp`/`rsync`/`install` followed by a destination is its source, not a target. A `cd` target and
  a `git -C` directory are still judged anywhere in the line.
- **Text is dropped.** Heredoc bodies and quoted strings that do not start with `/` or `~` are
  ignored; the heredoc's opening line and quoted paths are not. A command whose heredoc or quote
  is never closed is analysed as written, no rule applied.
- **A path starts with a real directory.** An absolute candidate whose first component is not an
  existing directory under `/` (`/`, `/clear`, `/^def`) is not a path.
- **Neutral redirections are not writes.** `N>&M`, `&>/dev/null`, `>/dev/null`.

What stays code, and is analysed like the rest of the command:

- **Quoted strings that run.** A string after `-c` (`-lc`…), `eval`, a word `sh`/`bash`/`zsh`/
  `ksh`/`dash`/`ssh`, `watch`, `parallel` or `<<<`, up to the next `;`, `|`, `&` or newline; a
  string piped into a word `sh`/`bash`… later in the command (`echo '…' | /bin/sh`, up to the next
  `;`, `&` or newline); and a string whose first word is a write command (`rm`, `mv`,
  `git commit`…, optionally after `sudo`/`env`/`nohup`/`time`).
- **Heredoc bodies that run.** A body is kept when a word `sh`/`ssh`/`eval`/`source`/`at`/`su`…
  or an input descriptor (`/dev/stdin`, `/dev/fd/N`) stands anywhere on its opening line
  (`bash <<EOF`, `cat <<EOF | sh`), or the opening line sits inside a `$(…)` or backtick
  substitution that holds such a word (`bash -c "$(` newline `cat <<EOF`). A line that ends with
  `\`, a pipe, `&&` or a redirection operator is joined to the next one: the body starts after
  the last. A `$((x << 4))` shift opens no heredoc.

Over-asks it accepts: a text that starts with `>` followed by an existing path; a file name that
holds the word `sh` (`cat <<EOF > run.sh`); the word `at`, `su` or `source` on a heredoc's opening
line, even inside a text; any string after `grep -c`, `git -c`…

What it does not see (use `permissions.deny` for a hard guarantee):

- a computed path (`$(…)`) or a write verb outside the list;
- code reached another way: `xargs sh -c` building its argument, a string stored in a variable,
  `python -c` writing with `open`, a command name held in a variable (`$cmd host 'rm …'`);
- a string that does not start with a write command, handed to any other runner
  (`some-runner "cmd && rm …"`);
- a script read by `python`, `perl`, `ruby` or `node` from a heredoc (it stays text);
- a path whose first component does not exist under `/`; a quoted `cd` or `git -C` target; a path
  under a system prefix written with `..` (`/tmp/../x`, not resolved before the prefix test).

## State is per session

`careful`, `freeze` and `deny` belong to the session that set them: another session of the same
project, or a later one, never inherits them. The state file
(`<project root>/.claude/.guard-state.json`) holds one entry per `session_id`. A sub-agent's tool
calls carry its parent's `session_id` (observed: a parent's `deny` blocked a sub-agent's `Write`),
so a sub-agent shares its parent's entry. The `SessionEnd` hook deletes the session's entry, and
any write prunes entries untouched for seven days (`SessionEnd` is not guaranteed to run). The
entry is dropped whenever the `SessionEnd` hook runs, whatever its reason. A
state file still in the earlier flat format is renamed to `.guard-state.json.legacy` by the first
write (unless one exists) and read as empty. The control script reads the session from
`CLAUDE_CODE_SESSION_ID` and exits with an error when it is not set — run it from the session's
own shell. `status` also prints the session's count of added directories and its short id, and
`guard off` clears `careful` and `freeze` but keeps the session's `added_dirs`.

The always-on outside-scope guard asks before a write outside the project root. It reads two
lists of directories that count as in scope: the ones a `/add-dir` typed in this session added
(filed per session by the `DirectoryAdded` hook), and `permissions.additionalDirectories` in
`.claude/settings.json`, `.claude/settings.local.json` and `~/.claude/settings.json`. A
`--add-dir` passed when `claude` starts emits no event and sits in no file, so it is not seen — a
write there asks. It asks for any unknown target and never denies one.

## Action

`$ARGUMENTS` is the mode word plus whatever that mode takes — pass it straight through, do not
reinterpret it.

Run the control script (use `py -3` instead of `python3` on Windows):

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/guard.py" $ARGUMENTS
```

Examples: `/permission-change careful on`, `/permission-change freeze src/core`,
`/permission-change deny secrets/`, `/permission-change guard off`.

To inspect the current state without changing it: `python3 "${CLAUDE_SKILL_DIR}/scripts/guard.py" status`.

Report the printed status line back to the user. The enforcement is the hook's job — do not
police commands or paths yourself.
