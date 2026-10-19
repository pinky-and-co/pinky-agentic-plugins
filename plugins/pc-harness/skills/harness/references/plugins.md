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

## Where a plugin keeps its state

Write plugin state under `${CLAUDE_PLUGIN_DATA}` (`~/.claude/plugins/data/<id>/`): Claude Code deletes
it on uninstall (not with `--keep-data`), a file elsewhere under `~/.claude/` stays behind. Hooks and
`stdio` MCP servers receive the variable; commands run through the Bash tool (a skill script) do not.
Unset → treat the state as absent and write nothing; never fall back to a `~/.claude/` path. Measured: a `stdio` MCP
server of a `directory` marketplace received it and wrote under `~/.claude/plugins/data/<plugin>-<marketplace>/`;
the deletion on uninstall is documented, not measured.
