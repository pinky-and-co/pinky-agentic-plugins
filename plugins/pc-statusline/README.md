# pc-statusline

Multi-line Claude Code status line — a main-session dashboard (model, effort, context bar, cache hit, cost, rate limits) and a per-subagent row.

## Overview

The plugin ships two status-line command scripts — `scripts/statusline.py` for the main session and `scripts/subagent_statusline.py` for subagents — plus a hook that keeps a quota cache fresh on a non-Anthropic provider. Both scripts read the JSON the harness sends on stdin and print; nothing is baked in (no language, no name, no path).

**Main session** (`scripts/statusline.py`) — a few rows of session state: the working folder and
git branch, the model with its effort and output style, the context window, the cache-hit rate,
the session cost (API-equivalent), the subagent count and the usage-limit projection. The set of
rows and their order follow what the harness provides and are not a contract. A Headroom row
joins them while its proxy is up (see below).

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

### Quota on a non-Anthropic provider

The harness only sends `rate_limits` for a claude.ai subscription. On another provider, the
plugin's hooks (`hooks/quota_refresh.py`, on session start, prompt, tool use and stop) fetch that
provider's own quota into a local cache, and the status line rebuilds the same limit projection
from it. The render path never touches the network, the token only ever goes to the provider's
own host, and on Anthropic the hook does nothing. Support for a provider is one entry in
`scripts/_quota.py` — one provider is supported, `api.z.ai`.

### Never crashes

A status line that errors blanks the whole bar, so both scripts are total: a malformed or
type-poisoned payload degrades to a bare fallback line, never an exception.

## Skills

None — the plugin ships status-line scripts and a hook only.

## Hooks

- **`quota_refresh.py`** (`hooks/quota_refresh.py`) — refreshes the active non-Anthropic provider's
  quota cache that the status line's rate-limit warning reads (`scripts/_quota.py`). Fires on
  `SessionStart`, `UserPromptSubmit`, `PostToolUse` and `Stop`, each run async with a 20 s timeout
  so it never adds latency. A no-op on Anthropic (its quota comes with the harness payload) and
  when the token is missing. Always exits 0 and prints nothing on stdout — for `SessionStart` and
  `UserPromptSubmit` the harness injects stdout into the model's context. Each event picks how
  young a cache is still good enough: session start always fetches, prompt/stop edges collapse
  duplicate firings (10 s), the tool edge throttles to 120 s.

## Requirements

Python 3.12 or newer, resolved as `python3` on the `PATH` — the status line command is
`python3 …/scripts/statusline.py`, run by the harness.

### Install

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

## Dependencies

None.
