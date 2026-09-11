#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp"]
# ///
"""pc-core model-routing MCP server — exposes `offline` and `adversarial` as native tools.

Each tool is a thin wrapper around a sibling module's functions (`model_offline.py`,
`model_adversarial.py`) — no reasoning happens here, this is the deterministic-fetch layer the LLM
boundary calls for. Neither tool spawns a model on Claude Code's side: `offline` calls a local Ollama
model, `adversarial` calls a non-Claude cloud model, and both return plain text — no subagent, no
extra model floor to declare.

Declared via `.mcp.json` at the plugin root (`command: uv, args: [run, --script, .../server.py]`) —
`uv` resolves the `mcp` dependency on first run from this file's own inline metadata above, no
install step for the plugin installer beyond having `uv`.
"""

from __future__ import annotations

import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import model_adversarial
import model_offline
import model_panel
import review_complexity
from mcp.server import MCPServer

m = MCPServer("pc-core-models")


@m.tool()
def offline(
    task: str,
    files: list[str] | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """Run a task on a local Ollama model, zero cloud egress.

    Use when a file's content must never reach the cloud model (privacy guard denial, sensitive
    data). Returns ONLY a status line + the local output path — never the answer content. Read the
    output file yourself only if you are explicitly allowed to see this material; the zero-egress
    invariant is enforced by this tool never returning the body, not by caller discipline.
    """
    files = files or []
    resolved_model = model_offline.resolve_model(model)
    url = model_offline.resolve_url()
    resolved_max_tokens = model_offline.resolve_max_tokens(max_tokens)

    hint = model_offline.preflight(url, resolved_model)
    if hint:
        return f"[offline] BLOCKED — {hint}"

    prompt = model_offline.build_prompt(task, files)
    t = time.time()
    timeout = max(900, resolved_max_tokens // 3)
    try:
        r = model_offline.call_ollama(
            url,
            resolved_model,
            prompt,
            model_offline.num_ctx_for(prompt, resolved_max_tokens),
            resolved_max_tokens,
            timeout,
        )
    except (urllib.error.URLError, OSError) as e:
        return f"[offline] ERROR — Ollama call failed: {e}"
    dur = time.time() - t

    out = model_offline.default_out()
    body = (
        r.get("response")
        or "(empty response — gemma drowns if `think` is not disabled)"
    )
    out.write_text(body, encoding="utf-8")

    return (
        f"[offline] {resolved_model} · {dur:.0f}s · in={r.get('prompt_eval_count')} "
        f"out={r.get('eval_count')} · done={r.get('done_reason')} → {out}"
    )


@m.tool()
def adversarial(
    task: str,
    files: list[str] | None = None,
    model: str | None = None,
    stack: str | None = None,
    tier: str | None = None,
    diff: str | None = None,
) -> str:
    """Challenge THIS session's own output with a contrarian second opinion from a non-Claude model.

    Pass your own plan/review/reasoning as `task` (plus source files if relevant) — a same-model
    self-check inherits its own blind spots, which is exactly what this tool exists to avoid. Tries
    each pool candidate in order, falling through to the next on failure; the returned text always
    opens with which model actually answered — never present a review as verdict without knowing
    which model produced it. The verdict itself is returned verbatim below that line; do not
    summarize or soften it on the model's behalf.

    `stack` (`fastapi` / `streamlit` / `snowflake`) appends that stack's review checklist to the
    methodology, since the pool model cannot invoke a skill itself. Omitted → inferred from import
    markers in `files`.

    `tier` (`haiku` / `sonnet` / `opus`) routes to the cheapest pool entry at or above that
    capacity — free before paid — instead of the default adversarial selection. `diff` (a path to
    a unified diff) derives that tier deterministically from the change size and risk factors when
    `tier` is not passed explicitly.
    """
    files = files or []
    override = model_adversarial.resolve_model(model)

    resolved_tier = tier if tier in model_adversarial._TIER_RANK else None
    if resolved_tier is None and diff:
        try:
            resolved_tier = review_complexity.tier_for(
                Path(diff).read_text(encoding="utf-8", errors="ignore")
            )
        except OSError:
            resolved_tier = None

    if override:
        entries = [
            {**model_adversarial._DEFAULT_POOL[0], "model": override, "name": override}
        ]
    elif resolved_tier:
        entries = [
            e
            for e in model_adversarial.select_for_delegation(
                model_adversarial.load_pool(), resolved_tier
            )
            if e["kind"] == "http"
        ]
    else:
        entries = model_adversarial.select_for_adversarial(
            model_adversarial.load_pool()
        )
    if not entries:
        return "[adversarial] BLOCKED — no candidate model in the pool."

    resolved_stack = stack or model_adversarial.detect_stack(files)
    methodology = model_adversarial.load_methodology(resolved_stack)
    adversarial_task = (methodology + "\n\n" if methodology else "") + task
    prompt = model_adversarial.build_prompt(adversarial_task, files)

    pool_result = model_adversarial.run_pool(entries, prompt)
    if pool_result.entry is None:
        return f"[adversarial] BLOCKED — every candidate failed. Last: {pool_result.result}"
    response = pool_result.result
    assert isinstance(response, dict)
    content = model_adversarial.extract_content(response)
    return f"[answered by {pool_result.entry['name']}]\n\n{content}"


@m.tool()
def panel(
    tasks: list[str],
    models: list[str] | None = None,
    tier: str | list[str] | None = None,
) -> str:
    """Send one prompt PER SLOT to a DIFFERENT model family each, in parallel; return every answer.

    `tasks` is one prompt per slot — `tasks[i]` goes to the i-th selected model. Use for a council
    of advisor voices (five lenses → five prompts → five models); for the same question to N models,
    repeat it (`["Q", "Q", "Q"]`). `len(tasks)` distinct families are selected from the pool, or
    `models` (by pool name, order kept). Unlike `adversarial`, no review posture is injected — each
    prompt is sent verbatim. Each answer is labelled with the model that produced it; the header
    reports answered/requested and names any slot skipped (no key / HTTP error / timeout — no retry)
    or left unfilled (pool has fewer eligible families than requested). `offline` entries are never
    used. By default only sonnet/opus-tier models are eligible; pass `tier` (a `capacity_tier` name
    or a list of them, e.g. `"haiku"`) to select from those tiers instead — for a non-advisor task
    that wants a small deterministic model.
    """
    if not tasks:
        return "[panel] BLOCKED — no task given."

    entries = model_panel.select_for_panel(
        model_adversarial.load_pool(), len(tasks), models, tier
    )
    if not entries:
        want = "sonnet/opus" if tier is None else tier
        return f"[panel] BLOCKED — no eligible model in the pool (need kind=http, tier {want})."

    response = model_panel.run_panel(entries, tasks[: len(entries)])
    if not response.answers:
        return f"[panel] BLOCKED — every candidate failed. Skipped: {response.skipped}"
    return model_panel.format_panel(response, len(tasks))


if __name__ == "__main__":
    m.run()
