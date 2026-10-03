# Testing for real

## Permission mode fallbacks

- **Haiku does not support `auto`**: `--model claude-haiku-4-5-20251001 --permission-mode auto`
  starts in `default` (debug log: `auto mode disabled: model … does not support auto mode`, then
  `Setting mode to 'default'`). A guard or mod tested that way does not reproduce a person whose
  `defaultMode` is `auto`. Test permission-dependent behaviour with a model that supports `auto`,
  and read the applied mode in the log before concluding.
- A mod tested only in `default` mode passed four interactive runs and failed in the person's real
  `auto` session: the mode is part of the setup, not a detail.

## Debug log

- `claude --debug-file <path>` writes a line per hook, mod event and permission decision. Useful
  greps: `hooks module <plugin> loaded`, `tool.check settled`, `ui.open`, `returned permissionDecision`,
  `Slow permission decision … (mode=…, behavior=…)`, `Setting mode to`.
- Without `--debug`, mod failures in a session that does not hot-reload the plugin go to the debug
  log only: no log, no trace.

## Driving a session

- In `auto` mode the classifier refuses an agent that types into another Claude Code session through
  `tmux send-keys` ("Tmux Self Drive"). Start the session in tmux, then hand the typing to the person
  (`tmux attach -t <name>` from their own terminal — not through a `!` command, which has no TTY).
- A fresh session per test: a session started before a change may keep the old module.

## `claude plugin eval`

Four traps that produce cheap, silent garbage rather than an error:

1. A case's `allowed_tools` frontmatter is not enough: pass `--allow-tools Bash,Write,Edit` on the
   command line too, or the tools are withheld and every grader needing them fails. A run that costs
   cents on a case doing real work means the tools were not granted.
2. `--judge-model` defaults to `haiku`: pass `--judge-model sonnet` from the first run.
3. The eval sandbox blocks outbound network at the OS level, with no flag to lift it: use a local
   `git init --bare` remote; PR/MR creation fails in-sandbox by design (grade the honest failure).
4. On macOS, the Xcode Command Line Tools `git` shells out to `xcrun`, whose cache write under
   `/var/folders/…` the sandbox denies: every `git` call exits 72. A self-contained `git` (Homebrew)
   is the candidate fix, untested.
