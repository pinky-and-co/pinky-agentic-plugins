# Plugins — what reloads after an edit

For a marketplace registered as a `directory`, `${CLAUDE_PLUGIN_ROOT}` resolves to the working tree,
not to `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` (measured on 2.1.283).

| Component | After an edit |
|---|---|
| Hook script | next matching tool call — re-executed every time |
| Skill `scripts/` | next invocation |
| Skill body, `hooks.json`, frontmatter | a new session or `/reload-plugins` |
| MCP server | **`/mcp` → reconnect the server** — `/reload-plugins` leaves the old process running |
| Mod module | a new session for certainty — after `/reload-plugins`, whether the new module runs is not established |
| Status line | the cache copy: refresh the cache, then re-point `statusLine` |

- **MCP server**: after editing its module, reconnect it through `/mcp`, then probe with a call that
  fails locally (a bad argument) before spawning anything that depends on it. The deferred-tool
  schema ToolSearch shows can stay stale for the session even then: probe the tool, not the schema.
- **Mod**: a session started before the mod existed showed no effect of it after `/reload-plugins`.
  Test a mod change in a fresh session (`claude --debug-file <log>`) and look for
  `hooks module <plugin> loaded … events: …` in the log.
- **When an edit seems ignored**, check what actually loaded it before refreshing anything: a
  skill's "Base directory" line, `ps` for an MCP server, the `statusLine` path, the debug log for a
  mod.
