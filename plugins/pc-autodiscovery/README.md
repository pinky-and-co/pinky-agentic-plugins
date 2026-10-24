# pc-autodiscovery

Always-on plugin activation: enables the project-scoped plugins matching wherever the session sits, and gates the next tool call while a user-only action (a slash command) is still pending.

## Overview

Hooks keep plugin activation in sync with the session's location: they activate matching project-scoped plugins when the session starts, enable more of them additively as the working directory moves, and restore the untouched state when the session ends. Actions only the user can perform (a slash command the CLI parses before a turn, such as `/reload-plugins`) go into a session-scoped queue that turns the agent's next tool call into a permission stop, so the request cannot scroll away while the agent keeps working. Two SessionStart companions nudge a commit when the only uncommitted change is a plugin activation, and regenerate the workspace and group `AGENTS.md` files on disk; the `agents-md` skill seeds a single repo's file. The activation mechanics live in this plugin rather than in pc-core so they stay loaded in exactly the session where pc-core is not loaded.

## Skills

### `agents-md`
Seeds a repo's `AGENTS.md` when it has none, folding `CONTEXT.md`'s glossary in and deleting `CONTEXT.md` once confirmed in the written file; with `--standalone`, renders the full standalone `AGENTS.md` for a repo leaving the workspace. Never overwrites an existing file without `--standalone` (a symlinked one is refused). The repo must have a `.git` entry; the workspace is resolved from `PINKY_WORKSPACE_ROOT` or the nearest `*.code-workspace`, or forced with `--workspace`.

```
/pc-autodiscovery:agents-md <repo-path>
```

## Hooks

Hooks live under `hooks/`; `pending_user_action.py` is the session-scoped queue library the activation hook and the gate share (importable, plus a CLI for skill bodies).

- `sync_plugin_scope.py` — fires on SessionStart, CwdChanged and SessionEnd. SessionStart (the ascend) activates every project-scoped plugin whose `projectPath` is an ancestor of the directory opened in, via `claude plugin install --scope project` — a versioned write to that directory's `.claude/settings.json`; a directory nested inside a git repo without being its root gets no write. CwdChanged matches against the new cwd additively at local scope (`claude plugin enable -s local`), never deactivating. SessionEnd restores `settings.local.json` from a shared per-directory backup once every session opened there has left, drops the per-session state, and clears the pending queue.
- `settings_commit_nudge.py` — fires on SessionStart. Tells the session to commit an uncommitted `.claude/settings.json` change, but only when it is unambiguous: that file is the sole dirty tracked file in the repo and the diff touches only `enabledPlugins` entries. Silent otherwise.
- `agents-md/agents_md_hook.py` — fires on SessionStart. Regenerates the workspace `AGENTS.md` and every group's, resolving the workspace from `PINKY_WORKSPACE_ROOT` or the nearest `*.code-workspace` (never a repo nor `$HOME`), and rewrites a file only when its content changed. A hand-written `##` section in a rewritten file is replaced — only the title and intro survive; a symlinked file is never touched. No workspace resolved, or no `tomllib` available, means a silent no-op.
- `pending_gate.py` — fires on PreToolUse (every tool call). When the queue holds a user-only action, turns the call into a permission stop carrying every queued message: `deny` for a pending `/reload-plugins` (the turn must end so the command can be typed), `ask` for anything else (approve-to-continue). Fails open — any internal error yields no decision.
- `clear_pending_queue.py` — fires on UserPromptSubmit. Clears the whole queue and the deny latch on the user's next message once a deny was actually issued; entries already shown to the user in-band are dropped on every prompt regardless.

## Requirements

- `python3` — stdlib only, no third-party packages. The `agents-md` SessionStart hook requires 3.11+ (`tomllib`) and no-ops on older interpreters; the `agents-md` skill script requires 3.12+.
- Claude Code CLI (`claude plugin enable` / `claude plugin install`) — the only write path `sync_plugin_scope.py` uses for activation.
- `git` — the nested-repo check and the commit nudge; a `.git` entry is required by the `agents-md` skill.
- No network access, no API keys, no local inference.

## Dependencies

None.
