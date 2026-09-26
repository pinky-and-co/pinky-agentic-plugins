#!/usr/bin/env python3
"""Adversarial runtime — run a task on a contrarian cloud model, from a configurable pool.

Called by the `adversarial` MCP tool (`mcp/pc_core_server.py`): it challenges **this session's own
output** (Claude), so a same-family reviewer is exactly what to avoid — the model here must not
share Claude's blind spots.

Dual-use: this module is both a standalone CLI (below) and an importable library for the MCP server,
which calls its functions directly rather than shelling out to a subprocess.

Pool: the ``model_pool`` key of ``settings.pinky.json``, resolved by the same ascending walk-up as
every other pinky setting (``policy_lib.load_policies`` — repo → parent directories → ``~/.claude/``),
seeded from ``CLAUDE_PROJECT_DIR`` (already present in this MCP server's own environment). No file
anywhere in the chain → a single built-in ``mistral-medium-3.5`` entry.

Every remote call goes through ``pi`` (``call_cli``): ``pi -p --model <provider>/<model>``. Provider
endpoints live in pi's registry (built-in, or a custom provider in ``~/.pi/agent/models.json``).
Keys come from the ``keys`` array of ``settings.pinky.json`` — the same keychain entries
``load_secrets`` exports for Bash — handed to each pi call as environment variables
(``secret_env``): this server's process never receives ``$CLAUDE_ENV_FILE``. A custom provider reads
its key with ``"apiKey": "$<NAME>"``. Adding a provider pi does not know is a ``models.json`` edit
plus a ``keys`` entry, never a code change here.

Tools are chosen per call, never per entry: none by default (``--no-tools``), or an allowlist
(``-t read,grep``) when the caller needs the model to look at files itself.

Each entry:
    name           display name
    kind           "cli" (called via pi), or "offline" (routed to the separate zero-egress
                   `offline` MCP tool instead — never called from here; see model_offline.py)
    provider       pi provider name — joined onto `model` as pi's `--model <provider>/<model>`,
                   also used for direct-vs-aggregator ordering
    model          provider-side model id — a router id (openrouter's `free`) answers with a
                   different underlying model on each call: fine for a mechanical check that only
                   asks whether the answer is right, wrong for anywhere the answering model itself
                   must stay fixed across calls, e.g. a benchmark
    capacity_tier  rough Claude-tier equivalence: "haiku" | "sonnet" | "opus"
    free_tier      true if this entry has no marginal cost (drives delegation's cost order)
    concurrency    max simultaneous tasks, or null for no known limit — recorded, not enforced
    local          true if this entry runs on this machine (e.g. MLX-SERVE) — absent/false means a
                   network call. Both selectors below tie-break on it so a local entry is tried
                   before an equally-ranked remote one, without special-casing a provider name.
    timeout        optional seconds to wait for this entry, overriding the caller's default — for a
                   slow provider that still answers well (NVIDIA NIM: 9–47s for a one-line reply)

Explicit ``--model``/``PINKY_ADVERSARIAL_MODEL`` override bypasses the pool and calls that Mistral
model id through pi.

Usage:
    model_adversarial.py --task "<task>" [--file PATH ...] [--model NAME] [--tool NAME ...]
    model_adversarial.py --check-pool
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal, NamedTuple

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
import load_secrets  # noqa: E402
from policy_lib import load_policies, resolve_repo_root  # noqa: E402

# Windows' default console codepage (cp1252, not UTF-8) can't encode this script's ✓/⚠ output.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

DEFAULT_MODEL = "mistral-medium-3.5"
CHECK_PROMPT = "Reply with exactly: OK"
CHECK_WORKERS = 4

_TIER_RANK = {"haiku": 0, "sonnet": 1, "opus": 2}

# Prédicats de bascule du pool (`run_pool`, plus bas). Un seul critère :
# bascule quand le fournisseur refuse de servir, jamais quand le modèle a servi mais répondu mal.
_RATE_LIMIT_RE = re.compile(r"\b429\b")
_OUT_OF_MEMORY_RE = re.compile(r"out_of_memory|GPU memory")
_INSUFFICIENT_CREDIT_RE = re.compile(r"\b402\b")
_TIMEOUT_RE = re.compile(r"\btimed out\b")


class PoolResult(NamedTuple):
    """Pool result: the entry that answered + its answer text, or None + the last error."""

    entry: dict[str, Any] | None
    result: str | None


class PoolCheck(NamedTuple):
    """One `check_pool` line: `ok` (answered), `failed` (with the reason), or `skipped`."""

    name: str
    status: Literal["ok", "failed", "skipped"]
    detail: str
    seconds: float


_DEFAULT_POOL: list[dict[str, Any]] = [
    {
        "name": DEFAULT_MODEL,
        "kind": "cli",
        "provider": "mistral",
        "model": DEFAULT_MODEL,
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
    `call_cli` (that would break its zero-egress guarantee).
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


_TIER_ORDER = ("opus", "sonnet", "haiku")


def select_for_delegation_with_fallback(
    pool: list[dict[str, Any]], tier: str
) -> list[dict[str, Any]]:
    """`select_for_delegation` at `tier`, degrading one tier at a time when the pool has
    nothing at or above it — a diff routed to `opus` (`review_complexity.tier_for`) must not
    go BLOCKED just because the configured pool tops out at `sonnet`, confirmed live: every
    entry in this workspace's own `settings.pinky.json` sits at `sonnet` or below.

    Restricted to `kind: "cli"` at every tier tried — the one kind callable through this pool
    path, `offline` going through the separate `offline` tool instead.
    """
    start = _TIER_ORDER.index(tier) if tier in _TIER_ORDER else 0
    for fallback_tier in _TIER_ORDER[start:]:
        candidates = [
            e for e in select_for_delegation(pool, fallback_tier) if e["kind"] == "cli"
        ]
        if candidates:
            return candidates
    return []


def resolve_model(cli_model: str | None = None) -> str | None:
    """CLI flag > PINKY_ADVERSARIAL_MODEL env > None (no override — use the pool)."""
    return cli_model or os.environ.get("PINKY_ADVERSARIAL_MODEL")


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
    """Whether `text` shows an MLX-SERVE memory refusal — either the preflight `503
    out_of_memory` (before the model loads) or the runtime `400` "requires ~NMB GPU memory but
    only ~MMB available" (the model is loaded, but this prompt's KV cache doesn't fit) — confirmed
    live: a large context size accepted at the config level still hits this second form under
    real memory pressure. Both mean the same thing to a caller: this entry can't serve right now.

    Args:
        text: the combined output of one `pi` invocation.

    Returns:
        True if either memory-refusal marker is present.
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


_HTTP_REFUSAL = (
    "kind 'http' is no longer supported — declare the provider in pi "
    "(~/.pi/agent/models.json) and set kind 'cli'"
)


def _not_callable(entry: dict[str, Any]) -> str:
    """Why `entry` cannot go through pi — the migration hint for a leftover `http` entry."""
    kind = entry.get("kind")
    if kind == "http":
        return f"{entry['name']}: {_HTTP_REFUSAL}"
    return f"{entry['name']}: kind {kind!r} is not callable through pi"


def entry_timeout(entry: dict[str, Any], default: int) -> int:
    """The entry's own `timeout` when it sets one, else the caller's `default`."""
    return int(entry.get("timeout", default))


def secret_env() -> dict[str, str]:
    """The keys declared in `settings.pinky.json` (`keys`), resolved from the keychain.

    Returns:
        `{VAR: value}` for every declared key that resolved — `load_secrets.resolve_env` on the
        settings seen from `CLAUDE_PROJECT_DIR`.
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    return load_secrets.resolve_env(load_secrets.load_keys(project_dir))


def call_cli(
    entry: dict[str, Any],
    prompt: str,
    timeout: int,
    tools: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """One `pi -p` call for a `kind: "cli"` entry; its stdout is the answer.

    Args:
        entry: the pool entry — `provider` and `model` form pi's `--model`.
        prompt: sent as the positional argument after `--`, so a prompt opening with `---` is
            never read as an option.
        timeout: seconds before `subprocess.TimeoutExpired`.
        tools: pi tool names to allow (`-t read,grep`); None or empty means `--no-tools`.
        env: variables layered over this process's environment for pi — the provider keys.
            None reads them with `secret_env`; a caller making several calls passes them once.

    Returns:
        The model's answer, as pi printed it.

    Raises:
        ValueError: the entry is not `kind: "cli"`.
        subprocess.CalledProcessError: pi exited non-zero (stdout/stderr carry the reason).
        subprocess.TimeoutExpired: no answer within `timeout`.
        OSError: `pi` is not installed or not executable.
    """
    if entry.get("kind") != "cli":
        raise ValueError(_not_callable(entry))
    tool_args = ["-t", ",".join(tools)] if tools else ["--no-tools"]
    cmd = [
        "pi",
        "-p",
        "--model",
        cli_model_id(entry),
        *tool_args,
        "--no-session",
        "--thinking",
        "off",
        "--",
        prompt,
    ]
    # stdin fermé : pi lit l'entrée standard quand il en hérite une, et celle du serveur MCP est
    # le canal JSON-RPC — l'appel resterait bloqué jusqu'au timeout.
    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        env={**os.environ, **(secret_env() if env is None else env)},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    ).stdout


def failure_detail(exc: subprocess.CalledProcessError) -> str:
    """The last non-empty line of a failed pi call's stdout + stderr — the provider's own error."""
    combined = f"{exc.stdout or ''}\n{exc.stderr or ''}"
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    return lines[-1] if lines else f"exit {exc.returncode}, no output"


def run_pool(
    entries: list[dict[str, Any]],
    prompt: str,
    timeout: int = 180,
    tools: list[str] | None = None,
) -> PoolResult:
    """Try each entry in order, first success wins.

    Returns (entry, answer) on success, or (None, last_error) once every entry has failed.
    A `kind: "offline"` entry is never dispatched here — it must go through the separate `offline`
    MCP tool, which enforces its own zero-egress guarantee (never returning the answer body).
    Shared by the CLI (`main`), the MCP tool and `pi_review`, so a fallback fix lands once.

    An entry advances to the next one only when `is_rate_limited` / `is_out_of_memory` /
    `is_insufficient_credit` / `is_timeout` say the provider refused to serve — never when it
    served but answered badly; any other non-zero exit is returned as a stop, not a fall-through.
    """
    last_error: str | None = None
    env = secret_env()
    for entry in entries:
        if entry["kind"] == "offline":
            last_error = (
                f"{entry['name']}: offline entries are not callable through the pool"
            )
            continue
        if entry["kind"] != "cli":
            last_error = _not_callable(entry)
            continue
        if not entry.get("model"):
            last_error = f"{entry['name']}: cli entries require a 'model' field"
            continue
        try:
            return PoolResult(
                entry,
                call_cli(entry, prompt, entry_timeout(entry, timeout), tools, env),
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
    return PoolResult(None, last_error)


def _check_one(entry: dict[str, Any], timeout: int, env: dict[str, str]) -> PoolCheck:
    """Send `CHECK_PROMPT` to one entry, with no tools, and time the answer."""
    if entry.get("kind") == "offline":
        return PoolCheck(
            entry["name"], "skipped", "offline — checked by `pinky doctor`", 0.0
        )
    timeout = entry_timeout(entry, timeout)
    start = time.monotonic()
    try:
        answer = call_cli(entry, CHECK_PROMPT, timeout, env=env).strip()
    except subprocess.CalledProcessError as e:
        detail = failure_detail(e)
    except subprocess.TimeoutExpired:
        detail = f"no answer within {timeout}s"
    except (OSError, ValueError) as e:
        detail = str(e)
    else:
        if answer:
            return PoolCheck(entry["name"], "ok", answer[:40], time.monotonic() - start)
        detail = "empty answer"
    return PoolCheck(entry["name"], "failed", detail, time.monotonic() - start)


def check_pool(
    pool: list[dict[str, Any]], timeout: int = 60, workers: int = CHECK_WORKERS
) -> list[PoolCheck]:
    """Ping every pool entry through pi, `workers` at a time, in pool order.

    Args:
        pool: the entries to check — `offline` ones are skipped (`pinky doctor` checks them).
        timeout: seconds allowed per entry.
        workers: concurrent pi calls.

    Returns:
        One `PoolCheck` per entry, in `pool` order.
    """
    env = secret_env()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda e: _check_one(e, timeout, env), pool))


_CHECK_MARKERS = {"ok": "✅", "failed": "❌", "skipped": "• "}


def render_pool_check(results: list[PoolCheck]) -> str:
    """The `--check-pool` report: an answered/called header, then one aligned line per entry.

    Args:
        results: what `check_pool` returned.

    Returns:
        The report text; a single header line when `results` is empty.
    """
    called = [r for r in results if r.status != "skipped"]
    answered = sum(1 for r in called if r.status == "ok")
    lines = [f"[pool] {answered}/{len(called)} answered"]
    width = max((len(r.name) for r in results), default=0)
    for r in results:
        text = f"{r.seconds:.1f}s" if r.status == "ok" else r.detail
        lines.append(f"{_CHECK_MARKERS[r.status]} {r.name.ljust(width)}  {text}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run a task on an adversarial cloud model (pool, or an explicit --model "
        "override), or check every pool entry."
    )
    ap.add_argument("--task")
    ap.add_argument("--file", action="append", default=[], dest="files")
    ap.add_argument("--model")
    ap.add_argument(
        "--tool",
        action="append",
        default=[],
        dest="tools",
        help="a pi tool the model may use (repeatable); none by default",
    )
    ap.add_argument(
        "--check-pool",
        action="store_true",
        help="send a one-line prompt to every pool entry and report which answer",
    )
    args = ap.parse_args(argv)

    if args.check_pool:
        results = check_pool(load_pool())
        print(render_pool_check(results))
        return 1 if any(r.status == "failed" for r in results) else 0
    if not args.task:
        ap.error("--task is required unless --check-pool is given")

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
    pool_result = run_pool(entries, prompt, tools=args.tools or None)
    if pool_result.entry is None:
        print(
            f"[adversarial] BLOCKED — every candidate failed. Last: {pool_result.result}",
            file=sys.stderr,
        )
        return 2

    print(
        f"[adversarial] {pool_result.entry['name']} · {time.time() - t:.0f}s",
        file=sys.stderr,
    )
    print(pool_result.result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
