# pc-models

Opt-in cross-model tools (`offline`, `adversarial`, `panel`, `pool_check`) served by one MCP server, with the reviewer gate and a live view of running calls.

## Overview

The plugin ships one MCP server (`pc-models`) exposing four cross-model tools, the `reviewer_gate` hook, and the `pi-view` mod that draws a running model call live in its transcript row. The idea: a same-model self-check inherits its own blind spots, so a second opinion comes from a non-Claude model — every call goes through `pi`, and no tool spawns a model on Claude Code's side. It holds its own copies of `policy_lib.py` (as `hooks/pc_models_policy_lib.py`) and the `review-guidelines` skill, so it runs from an installer cache without `pc-core`; a test keeps the `review-guidelines` copy byte-identical to `pc-core`'s. No other plugin depends on `pc-models`: without it, `finishing-a-card` has the `reviewer` agent review a diff.

## Skills

### `review-guidelines` *(reference)*

Shared review methodology — severity-labeled findings, high precision, a clear verdict, an optional adversarial stance. Applied by both the `reviewer` agent and the `adversarial` MCP tool; stack-specific guidance (FastAPI, Streamlit, Snowflake…) lives in the dedicated skills.

## Hooks

- `reviewer_gate.py` (PreToolUse on `Agent`) — denies the `reviewer` agent (or `pc-core:reviewer`) while the repo has a diff against its default branch and the session transcript holds no `adversarial` result opening with `[adversarial] BLOCKED` or `[adversarial] ERROR`; every other subagent type, and a repo with no diff, passes. Fails open on any git or transcript error; one BLOCKED or ERROR anywhere in the transcript allows the reviewer for the rest of the session.
- `pi-view` mod (`hooks/pi-view/register.tsx`, registered by `hooks/register.tsx`) — draws a running `adversarial`, `panel` or `offline` call like a subagent, from the view files the server writes for it (`offline`: a character count, never the text).

## MCP tools

Tool names carry the prefix `mcp__plugin_pc-models_pc-models__`.

- `offline` — runs a task on a local MLX-SERVE model via `pi`, zero cloud egress; returns a status line and the local output path, never the answer content.
- `adversarial` — contrarian second opinion on a change from a non-Claude pool model, called directly by the session that asks for the review; tries pool candidates in order, injects the shared review methodology (stack checklist via `stack`), and returns the verdict opened by which model answered, stripped of that model's reasoning traces.
- `panel` — sends one prompt per slot to a different model family each, in parallel, and returns every answer labelled with the model that produced it.
- `pool_check` — sends a one-line prompt to every `model_pool` entry through `pi`, no tools, and reports which answer (latency or the provider's own error); `tools=True` runs a fixed 12-step tool task instead and reports per entry `ok`, `429`, `tool-call text` or the provider's error. `offline` entries are skipped — `pinky doctor` checks those.

### Adversarial pool timeout

The `adversarial` pool has no wall-clock bound by default: a `pi` run is never cut by the pool while it progresses, tool loop included. `pi` itself bounds a blocked provider — its `httpIdleTimeoutMs` setting (300 000 ms of HTTP inactivity by default, `0` disables it), and `retry.provider.timeoutMs` for one request. No setting of `pi` bounds the total duration or the number of turns, so a tool loop that never ends is cut by nothing. A pool entry's `timeout` field (seconds) is the explicit option to bound one entry: it always cuts the run, kills the `pi` session, and drops the entry without a retry. The panel (180 s) and `pool_check` (60 s) keep their own delay; `offline` keeps its own. The server also caps each call at `PC_CORE_CALL_TIMEOUT_S`: at the cap it cancels the call's whole scope and kills every `pi` the call started.

## Requirements

- Python 3.12+ — the server (`requires-python = ">=3.12"`) and the hook scripts (stdlib only beyond the server's `mcp` dependency).
- `uv` — the server runs through `uv run --script`, which resolves its own dependencies.
- `pi` — every model call goes through it.
- git — `reviewer_gate.py` diffs the repo against its default branch.
- Local inference or third-party API keys — the pool is declared under `model_pool` in `.claude/settings.pinky.json`; keys come from the `keys` entries there.

## Dependencies

None.
