# pc-rtk

Rewrites Bash commands to their filtered [RTK](https://github.com/rtk-ai/rtk) form before they run, so only compact output enters the context.

## Overview

A `PreToolUse` hook on `Bash` rewrites a command to its filtered `rtk` form before it runs (`git log -n 3` becomes `rtk git log -n 3`), so only the compact output enters the context. It is a thin wrapper around RTK's own hook processor (`rtk hook claude`): the rewrite table lives in RTK, not here. The hook only sets the rewritten command; the permission checks apply to that command as they would to any other. It fails open: with no `rtk` on `PATH`, with an `rtk` that errors or lacks `hook claude`, or for a command RTK has no filter for, it exits 0 and the command runs unchanged.

## Skills

### `gain`

Show RTK's token-savings summary — commands filtered, tokens saved, per-command breakdown. Read-only. Use when the user asks how much RTK saved, or whether the pc-rtk rewrite hook is taking effect.

Only the read options of `rtk gain` pass through (`--project`, `--graph`, `--history`, `--quota`, `--tier`, `--daily`, `--weekly`, `--monthly`, `--all`, `--format`, `--failures`, `--recalls`); `--tier` only works together with `--quota`. Any other argument, `--reset` and `--yes` included, is refused with one line and exit 1, without calling `rtk`. A missing RTK prints one line telling you to run `brew install rtk` — never report zero savings.

Bare, or with the plain read flags (everything above except `--tier` and `--format`), the command runs without a model call; anything else, such as `-t` or `-f`, is answered by the skill through the model. The no-model run holds when `rtk` is installed; if `rtk` is missing or the script fails, the skill answers through the model instead. `rtk gain` prints a `No hook installed` warning because it does not see a hook provided by a plugin — expected, ignore it; the direct run shows only stdout, so the warning does not appear there.

```
/pc-rtk:gain --weekly
```

## Hooks

- **PreToolUse → Bash** — rewrites the command to its filtered `rtk` form before it runs, through RTK's own hook processor (`rtk hook claude`); 5 s timeout. Fails open: no `rtk` on `PATH`, an `rtk` that errors or lacks `hook claude`, or a command with no RTK equivalent leaves the command untouched and never blocks it.
- **UserPromptExpansion → `^pc-rtk:gain$`** — runs the deterministic `gain` script directly (`hooks/pc_rtk_direct_command_runner.py`, 30 s script timeout), with no model call, when `/pc-rtk:gain` is typed bare or with plain read flags; any other arguments fall back to the skill. It always fails toward the skill, never toward a block: a malformed grammar, a missing script, a non-zero exit or a timeout all print nothing and exit 0, so the skill then runs normally.

Bypass and recovery: `RTK_DISABLED=1 <cmd>` runs one command without the rewrite; `rtk recall` recovers the output a filter elided, by content hash.

`pi` reads no Claude Code hook; RTK ships its own extension for it. Run once: `rtk init -g --agent pi` — it writes a single file, `~/.pi/agent/extensions/rtk.ts`. Check it worked: run a verbose command (`git log -n 50`) in a `pi` session, then `rtk gain` — `Total commands` rises and `Tokens saved` is above 0.

## Requirements

- Python 3.12 or newer, resolved as `python3` on the `PATH`, for the hook runner; POSIX only.
- `bash`, to run the `gain` script.
- `rtk` on the `PATH`:

```bash
brew install rtk
```

## Dependencies

None.

**Enable** — from the workspace root, enable the plugin at `project` scope through the pinky CLI:

```bash
pinky claude-plugin install pc-rtk
```

It asks which folders enable the plugin; pick the workspace root.

**Teardown** — in this order:

1. Uninstall the plugin and drop its enables: `pinky claude-plugin disable pc-rtk`, selecting every folder where it is listed. For each selected folder it runs the project-scope `claude plugin uninstall` itself.
2. Remove the `pi` extension: `rtk init -g --agent pi --uninstall`.
3. Remove the binary: `brew uninstall rtk`.
4. Delete RTK's data directory — the folder next to the config file that `rtk config` prints (for example `~/Library/Application Support/rtk` on macOS), which holds `history.db`.
