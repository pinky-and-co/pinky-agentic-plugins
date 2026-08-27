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
    task: str, files: list[str] | None = None, model: str | None = None
) -> str:
    """Challenge THIS session's own output with a contrarian second opinion from a non-Claude model.

    Pass your own plan/review/reasoning as `task` (plus source files if relevant) — a same-model
    self-check inherits its own blind spots, which is exactly what this tool exists to avoid. Tries
    each pool candidate in order, falling through to the next on failure; the returned text always
    opens with which model actually answered — never present a review as verdict without knowing
    which model produced it. The verdict itself is returned verbatim below that line; do not
    summarize or soften it on the model's behalf.
    """
    files = files or []
    override = model_adversarial.resolve_model(model)
    entries = (
        [{**model_adversarial._DEFAULT_POOL[0], "model": override, "name": override}]
        if override
        else model_adversarial.select_for_adversarial(model_adversarial.load_pool())
    )
    if not entries:
        return "[adversarial] BLOCKED — no candidate model in the pool."

    # Shared methodology with the `reviewer` agent (`agents/reviewer.md`) — same review guidelines
    # and output format regardless of which model reviews, its own "Adversarial mode" section
    # always active here (this tool exists specifically for that stance).
    methodology = model_adversarial.load_methodology()
    adversarial_task = (methodology + "\n\n" if methodology else "") + task
    prompt = model_adversarial.build_prompt(adversarial_task, files)

    pool_result = model_adversarial.run_pool(entries, prompt)
    if pool_result.entry is None:
        return f"[adversarial] BLOCKED — every candidate failed. Last: {pool_result.result}"
    response = pool_result.result
    assert isinstance(response, dict)  # guardrail for mypy
    content = model_adversarial.extract_content(response)
    return f"[answered by {pool_result.entry['name']}]\n\n{content}"


if __name__ == "__main__":
    m.run()
