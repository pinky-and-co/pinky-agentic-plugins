#!/usr/bin/env python3
"""Adversarial runtime — run a task on a contrarian cloud model, from a configurable pool.

Called by the `adversarial` MCP tool (`mcp/server.py`): it challenges **this session's own output**
(Claude), so a same-family reviewer is exactly what to avoid — the model here must not share Claude's
blind spots.

Dual-use: this module is both a standalone CLI (below) and an importable library for
`mcp/server.py`, which calls its functions directly rather than shelling out to a subprocess.

Pool: the ``model_pool`` key of ``settings.pinky.json``, resolved by the same ascending walk-up as
every other pinky setting (``policy_lib.load_policies`` — repo → parent directories → ``~/.claude/``),
seeded from ``CLAUDE_PROJECT_DIR`` (already present in this MCP server's own environment — no
``project_dir``/``file_path`` parameter needed on the tool itself). No file anywhere in the chain →
a single built-in entry reproducing the original hardcoded default (``mistral-medium-3.5``), so an
unconfigured install behaves exactly as before. Adding a provider is a config edit only — this
module never special-cases a provider name; every entry carries its own call shape (OpenAI-compatible
``/v1/chat/completions``), key resolution, and capability/cost metadata.

Each entry:
    name           display name
    kind           "http" (called via call_provider) or "offline" (routed to the separate
                   zero-egress `offline` MCP tool instead — never called from here; see model_offline.py)
    provider       informational label, also used for direct-vs-aggregator ordering
    model          provider-side model id
    base_url       OpenAI-compatible chat-completions endpoint (absent/ignored for kind="offline")
    api_key_env    env var checked first
    api_key_file   fallback file (an unattended/scheduled caller has no shell env)
    capacity_tier  rough Claude-tier equivalence: "haiku" | "sonnet" | "opus"
    free_tier      true if this entry has no marginal cost (drives delegation's cost order)
    concurrency    max simultaneous tasks, or null for no known limit (offline: 1 — one local
                   machine; recorded for a future concurrent dispatcher, not enforced here — nothing
                   dispatches more than one task at a time yet)

Auth (per entry): ``api_key_env`` env var, else ``api_key_file`` on disk.

Explicit ``--model``/``PINKY_ADVERSARIAL_MODEL`` override bypasses the pool entirely and calls that
model id directly against the Mistral endpoint — the original single-provider override behavior,
unchanged.

Usage:
    model_adversarial.py --task "<task>" [--file PATH ...] [--model NAME]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
from policy_lib import load_policies, resolve_repo_root  # noqa: E402

# Windows' default console codepage (cp1252, not UTF-8) can't encode this script's ✓/⚠ output.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
DEFAULT_MODEL = "mistral-medium-3.5"
MAX_TOKENS = 8000
TEMPERATURE = 0.2

_TIER_RANK = {"haiku": 0, "sonnet": 1, "opus": 2}


class PoolResult(NamedTuple):
    """Pool result: entry + response, or None + error message."""

    entry: dict[str, Any] | None
    result: dict[str, Any] | str | None


_DEFAULT_POOL: list[dict[str, Any]] = [
    {
        "name": DEFAULT_MODEL,
        "kind": "http",
        "provider": "mistral",
        "model": DEFAULT_MODEL,
        "base_url": MISTRAL_API_URL,
        "api_key_env": "MISTRAL_API_KEY",  # pragma: allowlist secret
        "api_key_file": "~/.mistral_api_key",  # pragma: allowlist secret
        "capacity_tier": "opus",
        "free_tier": False,
        "concurrency": None,
    }
]


def load_pool() -> list[dict[str, Any]]:
    """The configured `model_pool`, or the single built-in Mistral entry if unconfigured.

    Same ascending `settings.pinky.json` resolution as every other pinky setting — no
    `project_dir`/`file_path` parameter on the caller, `CLAUDE_PROJECT_DIR` is already present in
    this MCP server's own process environment (set by the harness, same as for a hook subprocess).
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    repo_root = resolve_repo_root(project_dir, None)
    settings = load_policies(repo_root)
    if settings is None:
        return list(_DEFAULT_POOL)
    return settings.get("model_pool", list(_DEFAULT_POOL))


def _tier_rank(tier: str) -> int:
    return _TIER_RANK.get(tier, 0)


def select_for_adversarial(
    pool: list[dict[str, Any]], floor_tier: str = "sonnet"
) -> list[dict[str, Any]]:
    """Candidates for `adversarial`: never offline, at/above the capacity floor, direct before
    aggregator (an aggregator is API-usage-only, never the plan held directly with the provider —
    for a model reachable both ways, direct is at least as good, so it goes first).
    """
    candidates = [
        e
        for e in pool
        if e["kind"] != "offline"
        and _tier_rank(e["capacity_tier"]) >= _tier_rank(floor_tier)
    ]
    return sorted(candidates, key=lambda e: 0 if e["provider"] != "openrouter" else 1)


def select_for_delegation(
    pool: list[dict[str, Any]], task_tier: str = "haiku"
) -> list[dict[str, Any]]:
    """Candidates for task delegation: the cheapest entry that can still do the job — free before
    paid, and among equally-priced entries the one just capable enough (frugal: don't spend an
    opus-tier free model on a haiku-tier task when a haiku-tier free one qualifies).

    Offline is a real candidate here (free, `capacity_tier: haiku`) — a caller that resolves to a
    `kind: "offline"` entry must route to the separate `offline` MCP tool, never call it through
    `call_provider` (that would break its zero-egress guarantee).
    """
    candidates = [
        e for e in pool if _tier_rank(e["capacity_tier"]) >= _tier_rank(task_tier)
    ]
    return sorted(
        candidates,
        key=lambda e: (not e.get("free_tier", False), _tier_rank(e["capacity_tier"])),
    )


def resolve_model(cli_model: str | None = None) -> str | None:
    """CLI flag > PINKY_ADVERSARIAL_MODEL env > None (no override — use the pool)."""
    return cli_model or os.environ.get("PINKY_ADVERSARIAL_MODEL")


def _read_key_file(path: str) -> str | None:
    key_path = Path(path).expanduser()
    if key_path.exists():
        return key_path.read_text(encoding="utf-8").strip()
    return None


def resolve_api_key_for(entry: dict[str, Any]) -> str | None:
    """entry's api_key_env > entry's api_key_file. None if neither is set."""
    env_key = os.environ.get(entry["api_key_env"])
    if env_key:
        return env_key.strip()
    return _read_key_file(entry["api_key_file"])


def resolve_api_key() -> str | None:
    """Mistral key only — used by the explicit --model override path (bypasses the pool)."""
    return resolve_api_key_for(_DEFAULT_POOL[0])


_METHODOLOGY_PATH = (
    Path(__file__).parent.parent / "skills" / "review-guidelines" / "SKILL.md"
)
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n?", re.DOTALL)


def load_methodology() -> str:
    """The shared review methodology — the `review-guidelines` skill's body, frontmatter stripped.

    Single source of truth for what "reviewed" means in this suite: the `reviewer` agent and this
    tool both read the SAME skill (neither depends on the other — a skill file is neutral ground,
    same reasoning as `policy_lib.py` sitting between two hook concerns), so a finding looks the
    same regardless of which model produced it. Empty string if the file is missing rather than
    raising — a caller with no methodology loaded still gets a review, just an unstructured one.
    """
    try:
        text = _METHODOLOGY_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    return _FRONTMATTER_RE.sub("", text, count=1).strip()


def build_prompt(task: str, files: list[str]) -> str:
    """Assemble the prompt from the task + each file's content."""
    parts = [f"--- TASK ---\n{task}"]
    for p in files:
        content = Path(p).read_text(encoding="utf-8", errors="ignore")
        parts.append(f"--- FILE: {p} ---\n{content}")
    return "\n\n".join(parts)


def call_provider(
    base_url: str,
    api_key: str,  # gitleaks:allow
    model: str,
    prompt: str,
    timeout: int = 180,
) -> dict:
    """POST to any OpenAI-compatible /v1/chat/completions endpoint (non-streaming).

    `base_url` now comes from a config file rather than a hardcoded constant, so it is checked
    https-only before use — the trust level changed even though the file is user-owned, not
    attacker-supplied.
    """
    if not base_url.startswith("https://"):
        raise ValueError(f"refusing non-https base_url from pool config: {base_url}")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    req = urllib.request.Request(
        base_url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    return json.load(urllib.request.urlopen(req, timeout=timeout))  # nosec B310 — base_url is checked https-only above


def call_mistral(
    api_key: str,  # gitleaks:allow
    model: str,
    prompt: str,
    timeout: int = 180,
) -> dict:
    """Back-compat shim for the explicit --model override path (Mistral only)."""
    return call_provider(MISTRAL_API_URL, api_key, model, prompt, timeout)


def run_pool(
    entries: list[dict[str, Any]], prompt: str, timeout: int = 180
) -> PoolResult:
    """Try each entry in order, first success wins.

    Returns (entry, response) on success, or (None, last_error) once every entry has failed.
    A `kind: "offline"` entry is never dispatched here — it must go through the separate `offline`
    MCP tool, which enforces its own zero-egress guarantee (never returning the answer body).
    Shared by the CLI (`main`) and the MCP tool (`server.py`) so a fallback fix lands once, not twice.
    """
    last_error: str | None = None
    for entry in entries:
        if entry["kind"] == "offline":
            last_error = f"{entry['name']}: offline entries are not callable through the HTTP pool path"
            continue
        api_key = resolve_api_key_for(entry)  # pragma: allowlist secret
        if not api_key:
            last_error = f"{entry['name']}: no API key ({entry['api_key_env']} or {entry['api_key_file']})"
            continue
        try:
            r = call_provider(
                entry["base_url"], api_key, entry["model"], prompt, timeout
            )
        except urllib.error.HTTPError as e:
            last_error = f"{entry['name']}: HTTP {e.code}"
            continue
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            last_error = f"{entry['name']}: {e}"
            continue
        return PoolResult(entry, r)
    return PoolResult(None, last_error)


def extract_content(r: dict) -> str:
    """Pull the final answer text out of a chat-completions response.

    Reasoning models return content as a list of typed blocks
    [{"type":"thinking",...}, {"type":"text","text":...}] — keep only the final answer text.
    """
    choice = r.get("choices", [{}])[0]
    message = choice.get("message", {})
    content = (
        message.get("content") or message.get("reasoning_content") or "(empty response)"
    )
    if isinstance(content, list):
        texts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        content = "\n".join(t for t in texts if t) or "(no text block — reasoning only)"
    return content


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run a task on an adversarial cloud model (pool, or an explicit --model override)."
    )
    ap.add_argument("--task", required=True)
    ap.add_argument("--file", action="append", default=[], dest="files")
    ap.add_argument("--model")
    args = ap.parse_args(argv)

    override = resolve_model(args.model)
    prompt = build_prompt(args.task, args.files)

    if override:
        entries = [{**_DEFAULT_POOL[0], "model": override, "name": override}]
    else:
        entries = select_for_adversarial(load_pool())

    if not entries:
        print(
            "[adversarial] BLOCKED — no candidate model in the pool.", file=sys.stderr
        )
        return 2

    t = time.time()
    pool_result = run_pool(entries, prompt)
    if pool_result.entry is None:
        print(
            f"[adversarial] BLOCKED — every candidate failed. Last: {pool_result.result}",
            file=sys.stderr,
        )
        return 2

    dur = time.time() - t
    response = pool_result.result
    assert isinstance(response, dict)  # guardrail for mypy
    usage = response.get("usage", {})
    choice = response.get("choices", [{}])[0]
    print(
        f"[adversarial] {pool_result.entry['name']} · {dur:.0f}s · in={usage.get('prompt_tokens')} "
        f"out={usage.get('completion_tokens')} · finish={choice.get('finish_reason')}",
        file=sys.stderr,
    )
    print(extract_content(response))
    return 0


if __name__ == "__main__":
    sys.exit(main())
