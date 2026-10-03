# pc-statusline

A multi-line status line for Claude Code: a main-session dashboard and a per-subagent row. Pure
rendering — it reads the JSON the harness sends on stdin and prints; it holds no profile-resolved
value (no language, no name, no path baked in).

## What it shows

**Main session** (`scripts/statusline.py`) — four core rows, the first of which turns into a
`⚠ RATE LIMIT` banner above 90 % usage, plus a fifth Headroom row while its proxy is up:

1. Claude Code version (and, above 90 %, a `⚠ RATE LIMIT` banner with reset times)
1. model · effort · fast/normal · advisor model (when `--advisor` is active) · thinking · output style
1. session id · start time · subagent count
1. context-window bar + % · cache-hit rate · session cost (API-equivalent)
1. Headroom's net effect — only while its proxy is up (see below)

Rows are spaced with an invisible braille-blank line (the host collapses truly empty rows). The
right-hand column is placed from content width, not the terminal edge — a width read from `COLUMNS`
goes stale the moment a VS Code panel is resized.

### The Headroom row

[Headroom](https://github.com/headroomlabs-ai/headroom) is an optional third-party proxy that
compresses requests before they reach the API. Compression removes tokens, but rewriting a request
mutates the cached prefix and invalidates it — the tokens that would have been read at a discount
get re-sent instead. The row reports **the difference**, never the gross figure:

```
Headroom 🤑  ·  12% savings  ·  1336k tokens saved                       ← green, paying for itself
Headroom 💸  ·  1.8% losses  ·  286k tokens lost  ·  4 cache busts       ← red, costing more than it saves
```

The percentage is the net against what the session would have sent uncompressed, so it reads as
"N % cheaper" or "N % dearer". Its denominator counts only the requests Headroom actually
compressed, while busts are charged against the whole session — so a negative reading is slightly
pessimistic, never optimistic. The wording flips with the sign rather than staying constant: a
reader who has never heard of prompt caching should not have to work out what a minus means.

Tool-schema deferral is excluded on purpose. Routing through the proxy is what disables on-demand
tool loading in the first place (Headroom issue #746), so re-enabling it restores the unproxied
baseline rather than beating it — counting it would overstate the tool the same way a gross figure
does.

Counters come from the proxy's own `/stats` endpoint on `127.0.0.1:8787`, scoped to the proxy's
lifetime (`headroom wrap claude` ties that to one Claude Code session). The call is hard-bounded at
150 ms and memoized for 5 s, since a status line repaints on every turn. Proxy down, slow, or net
exactly zero → the row is absent, and nothing else on the bar changes.

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

That path carries the plugin version, so it stops matching the moment the plugin is upgraded — with
no error, since the old version's directory survives in the cache. Re-point it after every
`claude plugin update pc-statusline`. In this workspace `pinky claude-plugin install pc-statusline`
re-resolves it from the install registry.

Editing the plugin without bumping needs one more step: an install fills a cache directory only
when it is absent, so delete
`~/.claude/plugins/cache/<marketplace>/pc-statusline/<version>/` before re-running the install,
or the previous build keeps serving.

## Never crashes

A status line that errors blanks the whole bar, so both scripts are total: a malformed or
type-poisoned payload degrades to a bare fallback line, never an exception.
