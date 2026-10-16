---
name: harness
description: "Claude Code harness pitfalls measured in real sessions: writing or debugging a mod (function hooks, AbovePrompt band, Pane, Button, $.tool.check, $.session), how an installed plugin, MCP server or mod reloads after an edit, tools missing from a session (ENABLE_TOOL_SEARCH behind a third-party endpoint, Glob/Grep, LSP), testing a plugin or permission guard for real (permission modes, debug log, claude plugin eval). Use with plugin-authoring when building a mod, or when a hook, mod, MCP server or permission behaves differently than its docs say."
user-invocable: false
metadata:
  source: measured in interactive and headless Claude Code sessions (2.1.283 to 2.1.288), debug logs and the mods API typings
  updated: 2026-10-03
---

# Harness pitfalls (Claude Code)

What the docs and the bundled `plugin-authoring` skill do not say, or say in a way that is easy to
misread. Each entry was measured; the build it was measured on is named where it matters, since
the mods API is early access and moves between releases.

> Before trusting an entry, check the build (`claude --version`). On a newer build, re-measure the
> entry that decides your design, with a debug log (`--debug-file`), before building on it.

## References

- `references/mods.md` — writing a mod: what `$.tool.check` does not see, `$.session.cwd()` vs `root()`, band vs pane, Button styling and hotkeys, the test harness, layout of a mod inside a plugin, calling the plugin's own Python.
- `references/plugins.md` — what reloads after an edit: hooks, skills, MCP servers (`/mcp` reconnect, not `/reload-plugins`), mods, the status line cache.
- `references/tools.md` — tools missing from a session: `ENABLE_TOOL_SEARCH` behind a third-party endpoint dropping `Read`/`Write`/`Edit`, `Glob`/`Grep` absent, unwanted LSP plugin recommendations.
- `references/testing.md` — testing for real: permission mode fallbacks (no `auto` under Haiku), reading the debug log, driving a session, `claude plugin eval` traps.
