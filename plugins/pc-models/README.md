# pc-models

Opt-in cross-model tools for Claude Code, served by one MCP server (`pc-models`):

| Tool | What it does |
|---|---|
| `offline` | Runs a task on a local model; the answer goes to a file, never back into the session. |
| `adversarial` | Challenges the session's own work with a non-Claude pool model. |
| `panel` | Fans one prompt out to several pool models. |
| `pool_check` | Sends a one-line prompt to every `model_pool` entry and reports which answer. |

Tool names carry the prefix `mcp__plugin_pc-models_pc-models__`.

It also ships the `reviewer_gate.py` hook (denies the `reviewer` agent on a diff until `adversarial`
answered `BLOCKED` or `ERROR`) and the `pi-view` mod (draws a running `offline`, `adversarial` or
`panel` call live in its transcript row).

## Prerequisites

- `uv` — the server runs through `uv run --script`, which resolves its own dependencies.
- `pi` — every model call goes through it.
- Local inference or third-party API keys — the pool is declared under `model_pool` in
  `.claude/settings.pinky.json`; keys come from the `keys` entries there.

No other plugin depends on `pc-models`: without it, `finishing-a-card` has the `reviewer` agent
review a diff.

## Adversarial pool timeout

The `adversarial` pool has no wall-clock bound by default: a `pi` run is never cut by the pool
while it progresses, tool loop included. `pi` itself bounds a blocked provider — its
`httpIdleTimeoutMs` setting (300 000 ms of HTTP inactivity by default, `0` disables it), and
`retry.provider.timeoutMs` for one request. No setting of `pi` bounds the total duration or the
number of turns, so a tool loop that never ends is cut by nothing.

A pool entry's `timeout` field (seconds) is the explicit option to bound one entry: it always cuts
the run, kills the `pi` session, and drops the entry without a retry. The panel (180 s) and
`pool_check` (60 s) keep their own delay; `offline` keeps its own.

## Self-contained

The plugin holds its own copies of `policy_lib.py` (as `hooks/pc_models_policy_lib.py`) and the
`review-guidelines` skill, so it runs from an installer cache without `pc-core`. A test keeps the
`review-guidelines` copy byte-identical to `pc-core`'s.
