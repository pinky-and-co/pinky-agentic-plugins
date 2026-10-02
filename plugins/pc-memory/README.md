# pc-memory

Memory index recall, consultation counting and a guided review of the notes for Claude Code, as a plugin.

## Overview

A session opened in a sub-repo does not see the memory notes kept at the directory levels above
it. A `SessionStart` hook reads each level's `MEMORY.md` (read-only — it never writes to memory)
and emits:

- **To the user** (`systemMessage`): a tree of the levels, the widest first down to the current
  one, each nested under the previous. A level is a line with its directory name, its number of
  notes, `<K> old` when K notes are at least the stale threshold old (30 d by default) and the date
  of its last `reconcile` review (`-` when none). The current level ends with a pin. Under a
  level, its old notes are listed oldest first with their age, at most 10 followed by a `+N more`
  marker, each by its full path with the home directory written `~/` (a path outside the home
  directory stays absolute), as plain text, so a terminal that opens file paths on click can open
  it. The branch of a level closes on its last item only when no deeper level follows:

  ```text
  📚 Memory
  📂 pinky · 13 notes · 1 old · last review: -
  ├── ~/.claude/projects/-Users-x-pinky/memory/reference-automode-settings-location.md (35 d)
  └── 📂 bootstrap · 1 note · last review: -
        └── 📂 pinky-agentic-plugins · 22 notes · 1 old · last review: 2026-09-01 📍
              └── ~/.claude/projects/-Users-x-agentic-plugins/memory/feedback-probe-via-tool-call-path.md (34 d)

  💡 Audit your memory: /memory

  🔔 Clean it up: /pc-memory:reconcile (⚠️ You are on opus, best on sonnet · low effort)
  ```

  The list is built from the notes themselves, not from the index, so a level whose `MEMORY.md`
  cannot be read still gets its line and old notes. No index line is shown. The parts are
  separated by an empty line. The current level is always in the tree, with `0 notes` when it has
  no index; a parent level without an index is not. The tip is hidden when the `spinnerTipsEnabled` setting (the "Show
  tips" option of `/config`) is the boolean `false`. The bell line appears only when at least one
  level is due for review (see [Review reminder](#review-reminder)); the tip setting does not hide
  it. Not shown again after `/compact`.
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

`spinnerTipsEnabled` is read with the same priority as `autoMemoryDirectory`: the current level's
`.claude/settings.local.json`, then its `.claude/settings.json`, then the user `settings.json` in
the config directory; the first file holding a boolean wins. A missing key, a value that is not a
boolean (such as the string `"false"`) or an unreadable file keeps the tip. Not seen by the hook:
managed (enterprise) settings and the command-line `--settings` option, which can also impose the
setting.

A note's age is its frontmatter `modified` field, falling back to the file modification time.

On `resume`, as on a derivation (`fork`), no `SessionStart` hook runs again and the session keeps
the context of its history, parent indexes included, so the hook is wired to `startup`, `clear` and
`compact` only.

The hook never blocks the session: any error prints `{}` and exits 0.

### Review reminder

The startup message reminds the user to run `/pc-memory:reconcile` when a level is due for review.
A level is due when:

- its ledger `last_review` (set by `reconcile` for the levels it reviewed) is a `YYYY-MM-DD` date at
  least 30 days before today; or
- it was never reviewed (no `last_review`, or a missing, unreadable, corrupt or non-date ledger) and
  holds at least one note that is 30 days old or more. A never-reviewed level with only recent notes
  is not due, so a new directory is not flagged.

The threshold is `PC_MEMORY_REVIEW_DAYS` (30 by default), the same for both cases. One line closes
the message, after the tip, whatever the number of levels due: `🔔 Clean it up:
/pc-memory:reconcile (best on sonnet · low effort)`. When the `SessionStart` payload carries a
`model` string that does not contain `sonnet` (any case), the parenthesis starts with a warning
naming it, cleaned and cut at 40 characters: `(⚠️ You are on opus, best on sonnet · low effort)`. A
missing or non-string `model` gives no warning (Claude Code omits the field on some starts, for
example after `/clear`). The effort is not in the payload, so the line always advises the low one.
The model and the effort are advised because a skill's model override lasts a single turn and a
review spans several. The line is absent when no level is due, and, like the rest of the message,
after `/compact`. Reading the ledger never renames or rewrites it, even a corrupt one.

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

### Consultation counter

A `PostToolUse` hook (`Read`) counts, per note, in how many sessions it was consulted, so a later
review can tell the notes in use from the ones nobody opens. It acts only on a successful `Read`
of a `*.md` file located directly in a memory directory (the default silo of any project, or the
directory a level declares through `autoMemoryDirectory` in its `.claude/settings.local.json` or
in the user settings), `MEMORY.md` excluded. Any other `Read`
exits at once with no output; only a `.md` path costs a settings lookup.

The count lives in `.pc-memory-ledger.json` inside the memory directory, a dot file that is not a
note and never appears in the startup summary:

```json
{"notes": {"feedback_x.md": {"sessions": 3, "last_session": "<session id>", "last_read": "2026-10-02"}},
 "last_review": null}
```

- A counter goes up by one only when the session differs from the note's `last_session`: ten reads
  in one long session count once. `last_read` is the local date.
- The note and `MEMORY.md` are never written, so a read does not change the file it reads.
- Unknown keys and an existing `last_review` survive a rewrite. A ledger that cannot be decoded
  or parsed is rebuilt from the entries still usable, with a message on stderr, and the original
  is kept as `.pc-memory-ledger.json.corrupt` (an earlier `.corrupt` is overwritten). A ledger
  that cannot be read for another reason (a directory, no permission) is left as it is and
  nothing is counted.
- The counter writes into the directory, so it does not follow an `autoMemoryDirectory` declared
  in the level's shared `.claude/settings.json`, the file a cloned repo ships: a repo cannot
  redirect the write. A team that declares its memory directory there gets no count unless it
  also sets it in `settings.local.json` or the user settings. The default silos always count. The
  SessionStart and PreToolUse hooks still honour the shared file.
- Two sessions counting together lose nothing: the read-modify-write holds an exclusive `flock` on
  `.pc-memory-ledger.lock`, and the ledger is replaced atomically (temp file, then `os.replace`),
  so a reader never sees a truncated ledger. The lock is polled without blocking and given up
  after about 1 s: that count is then skipped (message on stderr), the tool call is not delayed
  further. Without `fcntl` (Windows) nothing is counted.
- A file named `.pc-memory-reconciling` in the memory directory suspends the count for that
  directory, as long as it is a regular file less than 10 minutes old (a marker left by a run that
  died stops mattering). It is checked under the lock: whoever creates the marker and then takes
  the lock is not overtaken by a count already in flight.
- The hook never blocks: any error goes to stderr, exit 0, nothing on stdout.

The counter is a biased proxy: a note is read only if its index line made the model open it. Read
it with the note's age, not on its own. The de-duplication compares with the last session only:
sessions A, B, A reading the same note count 3.

### Reconcile skill

`/pc-memory:reconcile` (user-invoked, never started by the model) reviews the notes of the current
level and of its parent levels, in the main session because it dialogues with the user. It runs two
deterministic scripts, with no model, kept in `skills/reconcile/scripts/`, and leaves the judgment to
the model:

1. `inventory.py [--cwd PATH]` prints JSON and writes nothing. Per level that has an index (the
   levels of the SessionStart hook, except that a directory declared only in the shared
   `.claude/settings.json`, which a cloned repo controls, is not listed: `apply.py` would refuse
   it): the level and its memory directory, `last_review` from the ledger, the index size
   (`lines`, `bytes`, `over_budget` past 200 lines or 25 KB, the part Claude Code reads), and per
   note the description, type, age in days (frontmatter `modified`, else the file mtime),
   `sessions`, `last_read`, whether the index links it, the first index line that does (null
   when none) and `coverage`. Every text field read from a note, an index or the ledger (descriptions, types, index
   lines, file names, orphan and duplicate names) has control characters removed and is capped at
   200 characters, descriptions and index lines also have their spaces normalised; `last_read` and
   `last_review` are kept only when they are `YYYY-MM-DD` dates, else null. Also the orphans (an index line whose file is gone, a note without
   index line) and the exact duplicates across all levels shown: the same body after the
   frontmatter, whitespace normalised, empty bodies not compared. Dot files, the ledger, its lock,
   `.corrupt` and temp files, the trash folder, non-`.md` entries and `MEMORY.md` are not notes. A
   level that cannot be read is skipped with a message on stderr.

   `coverage` is a hint that a note's fact is already stated where the instructions live, so the
   note can be deleted: up to 3 `{"file", "line", "text", "terms"}` (the line clipped to 160
   characters, cleaned, and the words that matched), best first. It is a clue: shared words are
   not a shared fact, so the skill reads the cited line before recommending a deletion. The sources are those of the root directory of every level shown:
   `CLAUDE.md`, `AGENTS.md`, `.claude/rules/*.md`, the string values of
   `.claude/settings.pinky.json`, every `skills/<name>/SKILL.md` below the root and the
   `description` fields of every `hooks/hooks.json`. Terms are the words of 6
   characters or more of the note's name and description and its backticked identifiers, minus
   common words and the repo's own vocabulary (plugin, hooks, skill, scripts, working, tree,
   files, repo, code, claude, session, memory, note, rule). A term found in more than 10% of the
   lines or of the files read (floor of 3) is common and weighs a tenth of a rare one. A source
   covers a note when a window of 3 lines holds at least 4 distinct rare terms, or one rare
   backticked identifier of 8 characters or more. The rule favours precision: a fact restated in
   other words with fewer rare terms is not reported. The work is bounded: 60 source files per
   level, none over 200 KB, nothing deeper than 6 directories. A file is read once through its real
   path, so a `CLAUDE.md` that links to `AGENTS.md` is not cited twice (the first name met is
   cited, `AGENTS.md` before `CLAUDE.md`); a link is followed only when its target stays inside the
   level's root, a link that leaves it or is broken is ignored, and no linked directory is
   entered. A line
   shaped like a secret (a long token after `:` or `=`, a known key prefix, a private-key header,
   credentials in a URL, a JWT) is never read. The detector is a local subset of the one in the
   `pc-core` plugin, which this plugin cannot import without requiring it to be installed.
   Nothing is written.
2. `apply.py --plan FILE|- [--dry-run] [--cwd PATH]` applies the plan the user approved (`-` reads
   it from standard input, which is how the skill passes it):

   ```json
   {"actions": [{"dir": "~/.claude/projects/x/memory", "note": "a.md",
                 "action": "delete" | "move" | "keep",
                 "to_dir": "~/.claude/projects/y/memory", "hard": false}],
    "reviewed_dirs": ["~/.claude/projects/x/memory"]}
   ```

   The whole plan is validated before anything is touched, and refused with exit 2 when:

   - a directory is not the memory directory of the current level or of a parent, as the
     inventory lists them (another project's silo is refused);
   - a note name is not a plain `*.md` file name (no separator, no leading dot, not `MEMORY.md`
     whatever its case), is a link to the index, or is neither a regular file nor a symlink;
   - two actions have the same source, or two moves the same destination;
   - a move has no destination, goes onto itself, or finds a different file at the destination
     (the message names both paths); identical bytes at both ends are a move that was
     interrupted, and it is finished;
   - an involved `MEMORY.md` is a symlink (it would be replaced by a regular file);
   - the JSON has a duplicated key, or is nested too deep;
   - a marker younger than 10 minutes is in an involved directory (another reconcile runs, or one
     was killed and its marker has not aged yet; the age and what to do are in the message).

   `delete` moves the note to `<dir>/.reconcile-trash/<YYYY-MM-DD>/` (a name taken that day gets a
   numeric suffix; the note is removed for good only with `"hard": true`), removes the index lines
   that link to it and prunes its ledger entry. `move` adds the index line to the destination
   index (the source line, or a link built from the note's description, with its brackets and
   parentheses removed and its target percent-encoded, so it makes one link), then copies the file to
   a hidden temporary file of `to_dir` and hard-links it to its final name: the link is atomic and
   never replaces a file, so the destination is never partial and a move between volumes works (a
   file system without hard links gets an exclusive copy instead). It then removes the temporary
   file and the source, removes the line from the source index and prunes the source ledger entry. A symlink note moves as a link. `keep` does
   nothing. A run killed without cleaning up
   (SIGKILL, power loss) leaves its marker: a new plan is refused for 10 minutes, then the same
   plan converges, without a duplicate or a partial file (tested by killing the run before each
   system call of a move and of a delete, and during the copy). Hidden `.pc-memory-move.*`,
   `.pc-memory-index.*` and `.pc-memory-ledger.*` temporaries a dead run left are removed under
   the lock. An index line that links several notes is removed only when the plan takes out all of
   them; otherwise it stays and the result carries `shared_index_line`. Then `last_review` (local
   date) is set in the ledger of each reviewed directory. A note already gone is reported
   `already_gone`. The summary is JSON on stdout; exit 1 when an action failed, with `results`
   (the failed action carries the `steps` it completed) and `left`, the actions not attempted.
   `--dry-run` prints every field of every action (`dir`, `note`, `action`, `to_dir`, `hard`) and
   `reviewed_dirs`, the plan as it will run, and changes nothing, not even the marker.

For the duration of `apply.py`, each involved directory holds a `.pc-memory-reconciling` marker,
created exclusively before anything else and removed in a `finally`, also when an action fails and
when the process gets SIGTERM or SIGHUP. The marker is refreshed before each action. It suspends
the consultation counter for that directory, and only the counter: the write guard does not look at
it, since `apply.py` names no memory path in its command. A marker older than 10 minutes was left
by a run that died (SIGKILL, power loss): the counter ignores it and the next application removes
it. The ledger is updated under the same `flock` the counter uses. The trash folder is the only
copy of a deleted note: nothing empties it.

The approval covers what runs: the skill shows the `--dry-run` output as it is in the approval
question, with permanent deletion as a separate option that is never the default, then runs exactly
that plan. `apply.py` is not in the skill's `allowed-tools`, so every run, the dry run included, goes
through the usual permission prompt, which shows the command and its plan.

The skill reads the `coverage` hints before judging a note and recommends deleting one that a
source already covers, citing the path and line. It asks the user in batches of at most 4
questions, opened by one intro line (`Batch 2/4 — 3 notes`): two or more covered notes go through
one grouped question first (delete all, one by one, keep all), and each other question gives the
note's title, its full path on its own line, a short summary, the covering source when there is
one, and a 1–2 word topic as its `header`.

The skill reads a note with `cat`, not the `Read` tool, so a review does not count as a
consultation, and a read refused by a confidentiality guard is never retried through another
channel. It edits nothing outside memory directories: a promotion (a skill, a hook, a rule, the
source of the instructions) is made by the session or the user, and only once they say it is done
does the skill propose deleting the note.

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
| `PC_MEMORY_REVIEW_DAYS` | `30` | a level whose last `reconcile` review is at least this many days old, or never reviewed with a note this old, triggers the review reminder |
| `PC_MEMORY_MODEL_LINES` | `30` | index lines injected per parent level into the model |
| `PC_ALLOW_MEMORY_WRITE` | unset | `1` lets memory writes through without asking; set it when launching the session |

## Limits

- A note's session count is not carried over when `reconcile` moves the note to another level: the
  destination starts at zero.

- The ledger is created with mode 0600; the lock file takes the default mode. A declared memory
  directory inside a repo or a synced folder receives a visible `.pc-memory-ledger.json` and
  `.pc-memory-ledger.lock`, and `flock` can be a no-op on some network or synced file systems, in
  which case two sessions counting together can lose an increment.
- A path spelled with another case on a case-insensitive file system (macOS) is not matched, as
  with the write guard.
- The counter sees what goes through the `Read` tool. A recall that bypasses it (a note loaded by the
  harness itself, a `cat` in `Bash`) is not counted.
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
