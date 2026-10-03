# Tools missing from a session

## `ENABLE_TOOL_SEARCH=true` drops `Edit` and `Write` (2.1.288)

With the flag in settings `env`, the main session gets ToolSearch, but its deferred list omits
`Edit`, `Write`, `Glob` and `Grep` while keeping `NotebookEdit`, `Read`, `Bash`, `LSP`, `Agent`.
Without the flag every native tool is exposed directly, but ToolSearch is gone (no on-demand MCP
loading). The trade-off is binary on this build.

- Subagents are not affected: their tools come from the `tools:` frontmatter of their definition.
- If `Edit`/`Write` are missing, check this flag before suspecting the workspace config. Native
  editing stays possible through a subagent that declares `Write`/`Edit`.

## `Glob` and `Grep` absent, cause unknown (2.1.288)

Seen behind a third-party model router in every configuration tested — with and without
`ENABLE_TOOL_SEARCH`, with and without `ENABLE_LSP_TOOL`. Neither flag is the cause. `rg`, `grep`
and `find` through `Bash` cover the need; truncate their output yourself.

## LSP plugin recommendations (2.1.283)

When it sees files of a language, Claude Code recommends an LSP plugin from a marketplace, without
checking that another plugin already serves that language. It recommends
`pyright-lsp@claude-plugins-official`, whose cache ships no `.lsp.json`; one accepted prompt
installs it at user scope next to a working fork.

- Controls in `~/.claude.json`: `lspRecommendationNeverPlugins` (plugin ids),
  `lspRecommendationDisabled` (bool), `lspRecommendationIgnoredCount`. Edit it with Claude Code
  closed — a running session rewrites that file.
- "2 plugin LSP servers" on `/reload-plugins` where one is expected: check `installed_plugins.json`
  for the recommended plugin first.
