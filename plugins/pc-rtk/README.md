# pc-rtk

[RTK](https://github.com/rtk-ai/rtk) output filtering for Claude Code, as a plugin.

## Overview

A `PreToolUse` hook on `Bash` rewrites a command to its filtered `rtk` form before it runs
(`git log -n 3` becomes `rtk git log -n 3`), so only the compact output enters the context. It is a
thin wrapper around RTK's own hook processor (`rtk hook claude`): the rewrite table lives in RTK,
not here. The hook only sets the rewritten command; the permission checks apply to that command as
they would to any other.

The hook fails open. With no `rtk` on `PATH`, with an `rtk` that errors or lacks `hook claude`, or
for a command RTK has no filter for, it exits 0 and the command runs unchanged.

## Prerequisite

```bash
brew install rtk
```

## Enable

From the workspace root, enable the plugin at `project` scope through the pinky CLI:

```bash
pinky claude-plugin install pc-rtk
```

It asks which folders enable the plugin; pick the workspace root.

## Skills

- **`/pc-rtk:gain`** — RTK's token-savings summary (`rtk gain`): commands filtered, tokens saved.
  Only the read options pass through (`--project`, `--graph`, `--history`, `--quota`, `--tier`,
  `--daily`, `--weekly`, `--monthly`, `--all`, `--format`, `--failures`, `--recalls`); any other
  argument, `--reset` included, is refused. `--tier` only works together with `--quota`. Prints one
  line telling you to run `brew install rtk` when RTK is missing.

  Bare, or with the plain read flags (everything above except `--tier` and `--format`), the command
  runs without a model call. Anything else, such as `-t` or `-f`, is answered by the skill through
  the model.

The no-model run holds when `rtk` is installed; if `rtk` is missing or the script fails, the skill
answers through the model instead.

`rtk gain` prints a `No hook installed` warning because it does not see a hook provided by a
plugin. It is expected; ignore it. The direct run shows only stdout, so the warning does not appear
there; the note applies to the model and shell path.

## Bypass and recovery

- `RTK_DISABLED=1 <cmd>` runs one command without the rewrite.
- `rtk recall` recovers the output a filter elided, by content hash.

## pi

`pi` reads no Claude Code hook; RTK ships its own extension for it. Run once:

```bash
rtk init -g --agent pi
```

It writes a single file, `~/.pi/agent/extensions/rtk.ts`. Check it worked: run a verbose command
(`git log -n 50`) in a `pi` session, then `rtk gain` — `Total commands` rises and `Tokens saved` is
above 0.

## Teardown

In this order:

1. Uninstall the plugin and drop its enables: `pinky claude-plugin disable pc-rtk`, selecting every
   folder where it is listed. For each selected folder it runs the project-scope
   `claude plugin uninstall` itself.
1. Remove the `pi` extension: `rtk init -g --agent pi --uninstall`.
1. Remove the binary: `brew uninstall rtk`.
1. Delete RTK's data directory — the folder next to the config file that `rtk config` prints (for
   example `~/Library/Application Support/rtk` on macOS), which holds `history.db`.
