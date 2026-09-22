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
    kind           "http" (called via call_provider), "cli" (called via pi), or "offline" (routed to the separate
                   zero-egress `offline` MCP tool instead — never called from here; see model_offline.py)
    provider       informational label, also used for direct-vs-aggregator ordering
    model          provider-side model id — a router id (openrouter's `free`) answers with a
                   different underlying model on each call: fine for a mechanical check that only
                   asks whether the answer is right, wrong for anywhere the answering model itself
                   must stay fixed across calls, e.g. a benchmark
    base_url       OpenAI-compatible chat-completions endpoint (absent/ignored for kind="offline")
    api_key_env    env var checked first; also the macOS keychain service name (see Auth below)
    capacity_tier  rough Claude-tier equivalence: "haiku" | "sonnet" | "opus"
    free_tier      true if this entry has no marginal cost (drives delegation's cost order)
    concurrency    max simultaneous tasks, or null for no known limit (offline: 1 — one local
                   machine; recorded for a future concurrent dispatcher, not enforced here — nothing
                   dispatches more than one task at a time yet)
    local          true if this entry runs on this machine (e.g. MLX-SERVE via `kind: "cli"`) —
                   absent/false means a network call. Both selectors below tie-break on it so a
                   local entry is tried before an equally-ranked remote one, without special-casing
                   `provider == "mlx"` by name.

Auth (per entry): ``api_key_env`` from the process env, else the macOS keychain item stored under
that same name. This server's process never receives ``$CLAUDE_ENV_FILE`` (Claude Code sources it
for Bash commands only, never for an MCP server), so the keychain is the real path — the env branch
only catches a value exported into the session by hand. Keychain reads need the ``login`` keychain
unlocked: a headless/SSH caller with it locked gets no key and the entry is skipped.

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
from keychain import read_macos_keychain  # noqa: E402
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

# Prédicats de bascule pour le chemin "cli" du pool (`run_pool`, plus bas). Un seul critère :
# bascule quand le fournisseur refuse de servir, jamais quand le modèle a servi mais répondu mal.
_RATE_LIMIT_RE = re.compile(r"\b429\b")
_OUT_OF_MEMORY_RE = re.compile(r"out_of_memory")
_INSUFFICIENT_CREDIT_RE = re.compile(r"\b402\b")
_TIMEOUT_RE = re.compile(r"\btimed out\b")


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


def tier_rank(tier: str) -> int:
    """Ordinal for a `capacity_tier` string (haiku < sonnet < opus); unknown tiers sort lowest.

    Public because `model_panel` shares the same tier ordering — one definition, both callers.
    """
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
        and tier_rank(e["capacity_tier"]) >= tier_rank(floor_tier)
    ]
    return sorted(
        candidates,
        key=lambda e: (
            0 if e.get("local", False) else 1,
            0 if e["provider"] != "openrouter" else 1,
        ),
    )


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
        e for e in pool if tier_rank(e["capacity_tier"]) >= tier_rank(task_tier)
    ]
    return sorted(
        candidates,
        key=lambda e: (
            not e.get("free_tier", False),
            tier_rank(e["capacity_tier"]),
            not e.get("local", False),
        ),
    )


def resolve_model(cli_model: str | None = None) -> str | None:
    """CLI flag > PINKY_ADVERSARIAL_MODEL env > None (no override — use the pool)."""
    return cli_model or os.environ.get("PINKY_ADVERSARIAL_MODEL")


def resolve_api_key_for(entry: dict[str, Any]) -> str | None:
    """`entry["api_key_env"]` from the process env, else the macOS keychain item of the same name.

    The keychain lookup keys on `api_key_env` directly — it works because this suite's `keys`
    array (`settings.pinky.json`) names each keychain service identically to its env var. Nothing
    enforces that: an item stored under a different service name would not be found.
    """
    env_key = os.environ.get(entry["api_key_env"])
    if env_key:
        return env_key.strip()
    return read_macos_keychain(entry["api_key_env"])


def cli_model_id(entry: dict[str, Any]) -> str:
    """The `--model` argument for a `kind: "cli"` pool entry: `<provider>/<id>`.

    Always joined, even when `entry["model"]` already contains a `/` (an org/name id, e.g. MLX's
    `mlx-community/Qwen3.5-9B-MLX-4bit` — `pi` accepts any number of segments after the first).
    Skipping this join lets `pi` guess among every provider that serves the same bare id: confirmed
    live, `openai/gpt-oss-120b` exists under both `groq` and `openrouter`, and an unprefixed
    `--model openai/gpt-oss-120b` resolved to a third provider, `baseten`, with no key configured
    — a routing bug that read as a missing-key error.
    """
    return f"{entry['provider']}/{entry['model']}"


def is_rate_limited(text: str) -> bool:
    """Whether `text` (a `pi` call's combined stdout+stderr) shows a 429 rate-limit error.

    Args:
        text: the combined output of one `pi` invocation.

    Returns:
        True if a `429` marker is present.
    """
    return bool(_RATE_LIMIT_RE.search(text))


def is_out_of_memory(text: str) -> bool:
    """Whether `text` shows the MLX-SERVE preflight `503 out_of_memory` signal.

    Args:
        text: the combined output of one `pi` invocation.

    Returns:
        True if an `out_of_memory` marker is present.
    """
    return bool(_OUT_OF_MEMORY_RE.search(text))


def is_insufficient_credit(text: str) -> bool:
    """Whether `text` shows a 402 — the provider refusing to serve for lack of balance, the same
    family as a 429 or an out-of-memory 503, never a bad answer from a model that did serve.

    Args:
        text: the combined output of one `pi` invocation.

    Returns:
        True if a `402` marker is present.
    """
    return bool(_INSUFFICIENT_CREDIT_RE.search(text))


def is_timeout(text: str) -> bool:
    """Whether `text` marks a `pi` call that ran past its time budget without answering — the
    model never served in time, the same family as a 429/503/402, never a bad answer once served.

    Args:
        text: the message built for a `subprocess.TimeoutExpired` (its own `str()` already reads
            "... timed out after N seconds").

    Returns:
        True if a timeout marker is present.
    """
    return bool(_TIMEOUT_RE.search(text))


_METHODOLOGY_PATH = (
    Path(__file__).parent.parent / "skills" / "review-guidelines" / "SKILL.md"
)
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n?", re.DOTALL)

_STACK_SKILLS: dict[str, tuple[str, str]] = {
    "fastapi": ("pc-craft", "fastapi"),
    "streamlit": ("pc-snow", "streamlit"),
    "snowflake": ("pc-snow", "snowflake"),
}

# Import-line patterns that identify a stack in the reviewed files, used only when the caller
# passes no explicit `stack`. Matched against whole file contents; anchored on `import`/`from` so a
# stray mention in a comment or string does not trigger. First stack with a match wins.
_STACK_MARKERS: dict[str, re.Pattern[str]] = {
    "fastapi": re.compile(r"^\s*(from fastapi\b|import fastapi\b)", re.MULTILINE),
    "streamlit": re.compile(r"^\s*(from streamlit\b|import streamlit\b)", re.MULTILINE),
    "snowflake": re.compile(r"^\s*(from snowflake\b|import snowflake\b)", re.MULTILINE),
}


def _stack_review_path(stack: str) -> Path | None:
    """Filesystem path of a stack's `references/review.md`, or None for an unknown stack key.

    `parents[2]` is the plugins root in dev (`<root>/plugins/pc-core/mcp/...`) and the marketplace
    root once installed (`<mp>/pc-core/mcp/...`) — sibling plugins sit next to `pc-core` in both.
    """
    spec = _STACK_SKILLS.get(stack)
    if spec is None:
        return None
    plugin, skill = spec
    return (
        Path(__file__).parents[2]
        / plugin
        / "skills"
        / skill
        / "references"
        / "review.md"
    )


def _load_stack_review(stack: str) -> str:
    """The stack's review reference body, or empty string if the key is unknown or the file absent."""
    path = _stack_review_path(stack)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def detect_stack(files: list[str]) -> str | None:
    """Best-effort stack key from import markers in the reviewed files.

    Used when the caller does not pass an explicit `stack`; first marker match wins, None when
    nothing matches. Unreadable files are skipped.
    """
    blob = ""
    for p in files:
        try:
            blob += Path(p).read_text(encoding="utf-8", errors="ignore") + "\n"
        except OSError:
            continue
    for stack, pattern in _STACK_MARKERS.items():
        if pattern.search(blob):
            return stack
    return None


def load_methodology(stack: str | None = None) -> str:
    """The shared review methodology — the `review-guidelines` skill's body, frontmatter stripped.

    Single source of truth for what "reviewed" means in this suite: the `reviewer` agent and this
    tool both read the SAME skill (neither depends on the other — a skill file is neutral ground,
    same reasoning as `policy_lib.py` sitting between two hook concerns), so a finding looks the
    same regardless of which model produced it. Empty string if the file is missing rather than
    raising — a caller with no methodology loaded still gets a review, just an unstructured one.

    `stack` (optional) appends that stack's `references/review.md` after the generic methodology,
    so a pool model with no `Skill` tool still gets the FastAPI / SiS / Snowflake checklist the
    `reviewer` agent would load itself. An unknown key or an uninstalled plugin adds nothing —
    same graceful-degrade as the methodology file.
    """
    try:
        text = _METHODOLOGY_PATH.read_text(encoding="utf-8")
        methodology = _FRONTMATTER_RE.sub("", text, count=1).strip()
    except OSError:
        methodology = ""
    if stack:
        extra = _load_stack_review(stack)
        if extra:
            return "\n\n".join(p for p in (methodology, extra) if p)
    return methodology


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
    max_tokens: int = MAX_TOKENS,
) -> dict:
    """POST to any OpenAI-compatible /v1/chat/completions endpoint (non-streaming).

    `base_url` now comes from a config file rather than a hardcoded constant, so it is checked
    https-only before use — the trust level changed even though the file is user-owned, not
    attacker-supplied.

    `max_tokens` overridable per call: Groq's free tier caps `openai/gpt-oss-120b` at 8000
    tokens/minute (TPM) — the global default alone exceeds that budget, confirmed via a real 413
    from the API, not assumed. A pool entry sets its own via `max_tokens` (see `run_pool`).
    """
    if not base_url.startswith("https://"):
        raise ValueError(f"refusing non-https base_url from pool config: {base_url}")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": TEMPERATURE,
        "max_tokens": max_tokens,
        "stream": False,
    }
    req = urllib.request.Request(
        base_url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # urllib's default User-Agent ("Python-urllib/x.y") gets blocked by some providers'
            # WAF (Cloudflare error 1010 on Groq, confirmed) — a plain UA string is enough to pass.
            "User-Agent": "pinky-adversarial/1.0",
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

    A `kind: "cli"` entry advances to the next pool entry only when `is_rate_limited` /
    `is_out_of_memory` / `is_insufficient_credit` / `is_timeout` say the provider refused to serve —
    never when it served but answered badly; any other non-zero exit is returned as a stop, not a
    fall-through.
    """
    last_error: str | None = None
    for entry in entries:
        if entry["kind"] == "offline":
            last_error = f"{entry['name']}: offline entries are not callable through the HTTP pool path"
            continue

        if entry["kind"] == "cli":
            # Call via pi: pi -p --model '<provider>/<id>' -t <tools> --no-session --thinking off "<prompt>"
            tools = entry.get("tools", "read")
            model = entry.get("model")
            if not model:
                last_error = f"{entry['name']}: cli entries require a 'model' field"
                continue

            import subprocess

            try:
                cmd = [
                    "pi",
                    "-p",
                    "--model",
                    cli_model_id(entry),
                    "-t",
                    tools,
                    "--no-session",
                    "--thinking",
                    "off",
                    prompt,
                ]
                res = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout, check=True
                )
                # pi output is expected to be the content directly
                return PoolResult(
                    entry, {"choices": [{"message": {"content": res.stdout}}]}
                )
            except subprocess.CalledProcessError as e:
                # Les deux flux, pas un seul : un fournisseur peut écrire son code d'erreur sur
                # stdout pendant que stderr porte autre chose.
                combined = f"{e.stdout or ''}\n{e.stderr or ''}"
                if (
                    is_rate_limited(combined)
                    or is_out_of_memory(combined)
                    or is_insufficient_credit(combined)
                    or is_timeout(combined)
                ):
                    last_error = f"{entry['name']}: {combined.strip()}"
                    continue
                return PoolResult(
                    None, f"{entry['name']} critical failure: {combined.strip()}"
                )
            except subprocess.TimeoutExpired as e:
                last_error = f"{entry['name']}: {e}"
                continue
            except OSError as e:
                last_error = f"{entry['name']}: {e}"
                continue

        api_key = resolve_api_key_for(entry)  # pragma: allowlist secret
        if not api_key:
            last_error = (
                f"{entry['name']}: no API key — {entry['api_key_env']} not in env, "
                f"and no keychain item of that name (or the keychain is locked)"
            )
            continue
        try:
            r = call_provider(
                entry["base_url"],
                api_key,
                entry["model"],
                prompt,
                timeout,
                entry.get("max_tokens", MAX_TOKENS),
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
