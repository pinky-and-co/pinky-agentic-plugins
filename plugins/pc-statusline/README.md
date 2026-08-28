# pc-statusline

A multi-line status line for Claude Code: a main-session dashboard and a per-subagent row. Pure
rendering — it reads the JSON the harness sends on stdin and prints; it holds no profile-resolved
value (no language, no name, no path baked in).

## What it shows

**Main session** (`scripts/statusline.py`) — four rows plus a conditional rate-limit banner:

1. Claude Code version (and, above 90 %, a `⚠ RATE LIMIT` banner with reset times)
1. model · effort · fast/normal · advisor model (when `--advisor` is active) · thinking · output style
1. session id · start time · subagent count
1. context-window bar + % · cache-hit rate · session cost (API-equivalent)

Rows are spaced with an invisible braille-blank line (the host collapses truly empty rows). The
right-hand column is placed from content width, not the terminal edge — a width read from `COLUMNS`
goes stale the moment a VS Code panel is resized.

**Subagents** (`scripts/subagent_statusline.py`) — replaces the default `name · description ·
tokens` row with `status · name · model · effort · context bar`, truncating the name to the
available width.

Both entry points share `scripts/_common.py` (ANSI palette, Windows/UTF-8 stdout setup, the bar
renderer). The deliberately divergent helpers — wide-char width, colour thresholds, empty-value
placeholders — stay per script.

## Install

```bash
claude plugin marketplace add <marketplace-source>
claude plugin install pc-statusline@<marketplace-name>
```

`subagentStatusLine` is wired automatically from this plugin's bundled `settings.json` once the
plugin is enabled. The main-session `statusLine` key is **not** readable from a plugin's bundled
settings (Claude Code only reads `agent` and `subagentStatusLine` there), so add it yourself to a
`settings.json` — pointing at `scripts/statusline.py` inside the installed plugin (its path is in
`~/.claude/plugins/installed_plugins.json`):

```json
{ "statusLine": { "type": "command", "command": "python3 \"<install-path>/scripts/statusline.py\"" } }
```

## Never crashes

A status line that errors blanks the whole bar, so both scripts are total: a malformed or
type-poisoned payload degrades to a bare fallback line, never an exception.
