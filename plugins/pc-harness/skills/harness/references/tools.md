# Tools missing from a session

## `ENABLE_TOOL_SEARCH=true` behind a third-party endpoint drops the native file tools (2.1.288)

Through a third-party Anthropic-compatible endpoint (`ANTHROPIC_BASE_URL` pointing elsewhere, here a
GLM model), the flag leaves the main session with `ToolSearch` and the deferred tools only: `Read`,
`Write`, `Edit` and `Grep` are gone, not even findable through `ToolSearch`. The same endpoint
without the flag keeps `Write` and `Edit`; the Anthropic API with the flag keeps `Edit`, `Write` and
`Grep` alongside `ToolSearch`.

- Keep `ENABLE_TOOL_SEARCH` out of a wrapper or alias that points Claude Code at another endpoint.
- If `Edit`/`Write` are missing, check the endpoint and this flag before suspecting the workspace
  config. Subagents are not affected: their tools come from the `tools:` frontmatter of their
  definition, so native editing stays possible through one that declares `Write`/`Edit`.

## `Glob` and `Grep` absent, cause unknown (2.1.288)

Absent without `ENABLE_TOOL_SEARCH` on the Anthropic API as on a third-party endpoint, with and
without `ENABLE_LSP_TOOL`; neither the flags nor the endpoint explain it. `rg`, `grep` and `find`
through `Bash` cover the need; truncate their output yourself.

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
