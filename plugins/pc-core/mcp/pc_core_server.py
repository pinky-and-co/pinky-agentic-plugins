#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp"]
# ///
"""pc-core model-routing MCP server — `offline`, `adversarial`, `panel` and `pool_check` as native tools.

Each tool is a thin wrapper around a sibling module's functions (`model_offline.py`,
`model_adversarial.py`, `model_panel.py`) — no reasoning happens here, this is the
deterministic-fetch layer the LLM boundary calls for. No tool spawns a model on Claude Code's side:
every model is called through `pi` (`offline` a local MLX-SERVE model, the others non-Claude pool
models), and every tool returns plain text — no subagent, no extra model floor to declare.

Declared via `.mcp.json` at the plugin root (`command: uv, args: [run, --script, .../server.py]`) —
`uv` resolves the `mcp` dependency on first run from this file's own inline metadata above, no
install step for the plugin installer beyond having `uv`.
"""

from __future__ import annotations

import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

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
    """Run a task on a local MLX-SERVE model via `pi`, zero cloud egress.

    Use when a file's content must never reach the cloud model (privacy guard denial, sensitive
    data). Returns ONLY a status line + the local output path — never the answer content. Read the
    output file yourself only if you are explicitly allowed to see this material; the zero-egress
    invariant is enforced by this tool never returning the body, not by caller discipline.
    """
    files = files or []
    resolved_model = model_offline.resolve_model(model)
    resolved_max_tokens = model_offline.resolve_max_tokens(max_tokens)

    prompt = model_offline.build_prompt(task, files)
    t = time.time()
    timeout = max(900, resolved_max_tokens // 3)
    try:
        body = model_offline.call_mlx(resolved_model, prompt, timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        fh = model_offline.failure_hint(e, resolved_model, timeout)
        label = "BLOCKED" if fh.code == 2 else "ERROR"
        return f"[offline] {label} — {fh.hint}"
    dur = time.time() - t

    out = model_offline.default_out()
    out.write_text(body or "(empty response)", encoding="utf-8")

    return f"[offline] {resolved_model} · {dur:.0f}s → {out}"


@m.tool()
def adversarial(
    task: str,
    files: list[str] | None = None,
    model: str | None = None,
    stack: str | None = None,
    tier: str | None = None,
    diff: str | None = None,
    tools: list[str] | None = None,
    base: str | None = None,
    scope: Literal["all", "staged", "unstaged"] = "all",
) -> str:
    """Contrarian second opinion on a change from a non-Claude model — called by the `reviewer`
    agent, which decides whether a review needs it; a session asking for a review spawns that agent
    rather than calling this directly.

    Pass the intent and what to attack as `task` (plus source files, or `base`) — a same-model
    self-check inherits its own blind spots, which is exactly what this tool exists to avoid. Tries
    each pool candidate in order, falling through to the next on failure; the returned text always
    opens with which model actually answered — never present a review as verdict without knowing
    which model produced it. The verdict itself is returned verbatim below that line; do not
    summarize or soften it on the model's behalf.

    `stack` (`fastapi` / `streamlit` / `snowflake`) appends that stack's review checklist to the
    methodology, since the pool model cannot invoke a skill itself. Omitted → inferred from import
    markers in `files`.

    Choice rule: a change with a diff always comes here — the reviewer reviews alone only a claim
    with no diff, or when this tool answers `BLOCKED` or `ERROR`. Whatever the path (no diff given
    to the tool, tier derived from the diff, explicit `tier`), the model is `sonnet` tier or above,
    never `haiku`; only `model` bypasses that floor.

    `tier` (`haiku` / `sonnet` / `opus`) routes to the cheapest pool entry at or above that
    capacity (raised to `sonnet` when lower) — free before paid — instead of the default
    adversarial selection. `diff` (a path to a unified diff) derives that tier deterministically
    from the change size and risk factors when `tier` is not passed explicitly.

    `tools` (pi tool names, e.g. `["read", "grep"]`) lets the model open files itself; omitted →
    no tools, the model sees only the prompt and the inlined `files`.

    `base` (a branch or ref, e.g. `main`; `HEAD` for work not yet committed) makes the tool compute
    the change under review itself: everything this work changed since it forked from `base` —
    commits, staged and unstaged edits, untracked files that are not git-ignored — is appended to
    the prompt, and so leaves the machine, and used to route the tier; the caller needs no diff
    file. It diffs the repository of this session's project directory, and names it in its
    messages. It takes precedence over `diff`. `scope` narrows it: `staged` (fork point → index),
    `unstaged` (index → working tree, untracked files included), or `all` (the default).
    """
    files = files or []
    override = model_adversarial.resolve_model(model)

    diff_text: str | None = None
    repo = model_adversarial.project_dir()
    if base:
        try:
            repo = model_adversarial.repo_root()
            diff_text = model_adversarial.diff_against(base, scope=scope)
        except subprocess.CalledProcessError as e:
            return f"[adversarial] ERROR — cannot diff against {base!r} in {repo}: {model_adversarial.failure_detail(e)}"
        except (ValueError, OSError) as e:
            return f"[adversarial] ERROR — cannot diff against {base!r} in {repo}: {e}"
        if not diff_text.strip():
            return f"[adversarial] nothing to review — no change against {base!r} in {repo}."
    elif diff:
        try:
            diff_text = Path(diff).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            diff_text = None

    resolved_tier = tier if tier in model_adversarial._TIER_RANK else None
    if resolved_tier is None and diff_text:
        resolved_tier = review_complexity.tier_for(diff_text)

    if override:
        entries = [
            {**model_adversarial._DEFAULT_POOL[0], "model": override, "name": override}
        ]
    elif resolved_tier:
        entries = model_adversarial.select_for_delegation_with_fallback(
            model_adversarial.load_pool(), resolved_tier, floor_tier="sonnet"
        )
    else:
        entries = model_adversarial.select_for_adversarial(
            model_adversarial.load_pool(), rng=random.Random()
        )
    if not entries:
        return "[adversarial] BLOCKED — no candidate model in the pool."

    resolved_stack = stack or model_adversarial.detect_stack(files)
    methodology = model_adversarial.load_methodology(resolved_stack)
    adversarial_task = (methodology + "\n\n" if methodology else "") + task
    if base and diff_text:
        adversarial_task += f"\n\n--- DIFF against {base} in {repo} ---\n{diff_text}"
    prompt = model_adversarial.build_prompt(adversarial_task, files)

    pool_result = model_adversarial.run_pool(entries, prompt, tools=tools)
    if pool_result.entry is None:
        return f"[adversarial] BLOCKED — every candidate failed. Last: {pool_result.result}"
    return f"[answered by {model_adversarial.answer_label(pool_result.entry)}]\n\n{pool_result.result}"


@m.tool()
def panel(
    tasks: list[str],
    models: list[str] | None = None,
    tier: str | list[str] | None = None,
    tools: list[str] | None = None,
) -> str:
    """Send one prompt PER SLOT to a DIFFERENT model family each, in parallel; return every answer.

    `tasks` is one prompt per slot — `tasks[i]` goes to the i-th selected model. Use for a council
    of advisor voices (five lenses → five prompts → five models); for the same question to N models,
    repeat it (`["Q", "Q", "Q"]`). `len(tasks)` distinct families are selected from the pool, or
    `models` (by pool name, order kept). Unlike `adversarial`, no review posture is injected — each
    prompt is sent verbatim. Each answer is labelled with the model that produced it; the header
    reports answered/requested and names any slot skipped (pi error / timeout — no retry)
    or left unfilled (pool has fewer eligible families than requested). `offline` entries are never
    used. By default only sonnet/opus-tier models are eligible; pass `tier` (a `capacity_tier` name
    or a list of them, e.g. `"haiku"`) to select from those tiers instead — for a non-advisor task
    that wants a small deterministic model. `tools` (pi tool names) is given to every slot;
    omitted → no tools.
    """
    if not tasks:
        return "[panel] BLOCKED — no task given."

    entries = model_panel.select_for_panel(
        model_adversarial.load_pool(), len(tasks), models, tier
    )
    if not entries:
        want = "sonnet/opus" if tier is None else tier
        return f"[panel] BLOCKED — no eligible model in the pool (need kind=cli, tier {want})."

    response = model_panel.run_panel(entries, tasks[: len(entries)], tools=tools)
    if not response.answers:
        return f"[panel] BLOCKED — every candidate failed. Skipped: {response.skipped}"
    return model_panel.format_panel(response, len(tasks))


@m.tool()
def pool_check(provider: str | None = None, names: list[str] | None = None) -> str:
    """Send a one-line prompt to every `model_pool` entry through `pi`, with no tools, and report
    which answer: one line per entry with its latency, or the provider's own error (bad key, rate
    limit, unknown model, timeout). `offline` entries are skipped — `pinky doctor` checks those.

    `provider` and/or `names` narrow the check to the entries just added — each entry checked is
    one real call against that provider's quota.
    """
    pool = model_adversarial.filter_pool(model_adversarial.load_pool(), provider, names)
    if not pool:
        return model_adversarial.filter_miss(provider, names)
    results = model_adversarial.check_pool(pool)
    return model_adversarial.render_pool_check(results)


if __name__ == "__main__":
    m.run()
