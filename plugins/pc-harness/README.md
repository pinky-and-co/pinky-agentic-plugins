# pc-harness

Claude Code harness knowledge measured in real sessions — public, generic across any project that builds plugins, hooks or mods for Claude Code.

## Overview

The plugin ships one model-invoked reference skill that records where the Claude Code docs and the bundled `plugin-authoring` skill are silent or easy to misread: the mods API, plugin and MCP reload behavior, tool exposure, and testing in real sessions. Every entry is measured — interactive and headless sessions, debug logs, the mods API typings — and carries the build it was measured on, since the mods API is early access and moves between releases; the entry that decides a design is re-measured on the current build (`claude --version`) before it is trusted. The skill is read before building on that part of the harness rather than rediscovered.

## Skills

### `harness` *(reference)*

Claude Code harness pitfalls measured in real sessions: writing or debugging a mod (function hooks, AbovePrompt band, Pane, Button, `$.tool.check`, `$.session`), how an installed plugin, MCP server or mod reloads after an edit, tools missing from a session (`ENABLE_TOOL_SEARCH` behind a third-party endpoint, `Glob`/`Grep`, LSP), and testing a plugin or permission guard for real (permission modes, debug log, `claude plugin eval`). One reference file per topic under `skills/harness/references/`.

## Requirements

None — prompt-only skills.

## Dependencies

None.
