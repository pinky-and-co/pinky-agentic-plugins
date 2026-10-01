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
anywhere in the chain → a single built-in ``nvidia/z-ai/glm-5.3`` entry.

Every remote call goes through ``pi`` (``call_cli``): ``pi -p --model <provider>/<model>``. Provider
endpoints live in pi's registry (built-in, or a custom provider in ``~/.pi/agent/models.json``).
Keys come from the ``keys`` array of ``settings.pinky.json`` — the same ``macos_keychain`` entries
``load_secrets`` declares — resolved on demand and handed to each pi call as environment variables
(``secret_env``), never exported into ``$CLAUDE_ENV_FILE``: this server's process never receives
that file, and a keychain key is never held in a session's own env either. A custom provider reads
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
    timeout        optional wall-clock seconds for the whole `pi` run of this entry (tool loop
                   included), overriding the caller's default — a backstop against a run that never
                   ends; `pi` bounds each request itself
    priority       optional integer, default 0, higher first: orders the providers inside each round
                   of the selection's provider alternation — a generous provider opens every round
                   without stacking its entries. Never outranks locality, cost or tier.
    thinking       optional pi thinking level (`off`, `low`, `medium`, …); absent → pi's own default.
                   `off` for a small local model, which a thinking pass drowns into an empty answer

Explicit ``--model``/``PINKY_ADVERSARIAL_MODEL`` override bypasses the pool and calls that Mistral
model id through pi.

Usage:
    model_adversarial.py --task "<task>" [--file PATH ...] [--model NAME] [--tool NAME ...]
    model_adversarial.py --check-pool [--provider NAME] [--name ENTRY ...]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import itertools
import os
import re
import stat
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, NamedTuple

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
import load_secrets  # noqa: E402
from policy_lib import load_policies, resolve_repo_root  # noqa: E402

# Windows' default console codepage (cp1252, not UTF-8) can't encode this script's ✓/⚠ output.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

DEFAULT_MODEL = "z-ai/glm-5.3"
RUN_TIMEOUT = 900
CHECK_PROMPT = "Reply with exactly: OK"
CHECK_WORKERS = 4

_TIER_RANK = {"haiku": 0, "sonnet": 1, "opus": 2}
# Selection ranks: sonnet and opus are one capacity class — only a few entries carry the `opus`
# label, and a queue built on it alone is held by whichever provider they share.
_SELECTION_RANK = {"haiku": 0, "sonnet": 1, "opus": 1}

# Prédicats de bascule du pool. Rate limit, out of memory et insufficient credit sont définitifs
# (abandon sans retour) ; timeout, comme une réponse vide, est transitoire (un seul retour en fin
# de file).
_RATE_LIMIT_RE = re.compile(r"\b429\b")
_OUT_OF_MEMORY_RE = re.compile(r"out_of_memory|GPU memory")
_INSUFFICIENT_CREDIT_RE = re.compile(r"\b402\b")
_ORGANIZATION_LIMIT_RE = re.compile(r"\borgani[sz]ation reached\b", re.IGNORECASE)
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
        "name": "nvidia-glm-5.3",
        "kind": "cli",
        "provider": "nvidia",
        "model": DEFAULT_MODEL,
        "capacity_tier": "opus",
        "free_tier": True,
        "concurrency": None,
        "timeout": 300,
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


def selection_rank(tier: str) -> int:
    """Ordinal of a `capacity_tier` for candidate selection: sonnet and opus rank the same,
    haiku below; unknown tiers sort lowest."""
    return _SELECTION_RANK.get(tier, 0)


def spread_providers(
    entries: list[dict[str, Any]], key: Callable[[dict[str, Any]], Any]
) -> list[dict[str, Any]]:
    """`entries` sorted by `key`, each group of equal `key` then taken round-robin across
    providers — never two entries of one provider back to back while another provider still has
    one in the group.

    A provider that is down or slow (NVIDIA NIM: 9–47s for a one-line reply) fails all its
    entries alike; consecutive ones burn a full timeout each before any other provider is tried.
    `key` decides which group comes first, so spreading never reorders across locality, cost or
    aggregator priority. Within a group, entries are taken in descending `priority` (pool order
    between equals): the provider holding the highest one opens each round, and a provider's own
    entries come out highest `priority` first.
    """
    spread: list[dict[str, Any]] = []
    for _, group in itertools.groupby(sorted(entries, key=key), key=key):
        by_provider: dict[str, deque[dict[str, Any]]] = {}
        for e in sorted(group, key=lambda e: -e.get("priority", 0)):
            by_provider.setdefault(e["provider"], deque()).append(e)
        queues = list(by_provider.values())
        while queues:
            spread.extend(q.popleft() for q in queues)
            queues = [q for q in queues if q]
    return spread


def select_for_adversarial(
    pool: list[dict[str, Any]], floor_tier: str = "sonnet"
) -> list[dict[str, Any]]:
    """Candidates for `adversarial`: never offline, at/above the capacity floor (sonnet and opus
    count the same), direct before aggregator (an aggregator is API-usage-only, never the plan held
    directly with the provider — for a model reachable both ways, direct is at least as good, so
    it goes first), providers alternating inside each of those groups (`spread_providers`).
    """
    candidates = [
        e
        for e in pool
        if e["kind"] != "offline"
        and selection_rank(e["capacity_tier"]) >= selection_rank(floor_tier)
    ]
    return spread_providers(
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

    Sonnet and opus count as one tier (`selection_rank`); providers alternate inside each group of
    equal cost, tier and locality (`spread_providers`).

    Offline is a real candidate here (free, `capacity_tier: haiku`) — a caller that resolves to a
    `kind: "offline"` entry must route to the separate `offline` MCP tool, never call it through
    `call_cli` (that would break its zero-egress guarantee).
    """
    candidates = [
        e
        for e in pool
        if selection_rank(e["capacity_tier"]) >= selection_rank(task_tier)
    ]
    return spread_providers(
        candidates,
        key=lambda e: (
            not e.get("free_tier", False),
            selection_rank(e["capacity_tier"]),
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


_TIER_BADGES = {"haiku": "🟦", "sonnet": "🟪", "opus": "🟧"}


def answer_label(entry: dict[str, Any]) -> str:
    """How a reply names the entry that produced it: `provider/model · <badge> <tier>`.

    The model id is cut to its last path segment (`z-ai/glm-5.3` → `glm-5.3`). The tier is the only
    ordinal attribute, so it alone carries a colour — squares, never the circles that mark a
    finding's severity in a review.

    Args:
        entry: the pool entry that answered.

    Returns:
        The label; a tier with no badge is shown as plain text.
    """
    who = f"{entry['provider']}/{entry['model'].split('/')[-1]}"
    tier = entry.get("capacity_tier")
    if not tier:
        return who
    badge = _TIER_BADGES.get(tier)
    return f"{who} · {badge} {tier}" if badge else f"{who} · {tier}"


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


def is_provider_wide_limit(text: str) -> bool:
    """Whether `text` shows a refusal that covers every model of the provider for this run: a 402
    (no balance) or a 429 worded for the whole organization (`Your organization reached its limit
    …`). A 429 that names one model — a per-model quota, a tokens-per-minute budget — says nothing
    about the provider's other models.

    Args:
        text: the combined output of one `pi` invocation.

    Returns:
        True if the limit is the provider's, not one model's.
    """
    if is_insufficient_credit(text):
        return True
    return is_rate_limited(text) and bool(_ORGANIZATION_LIMIT_RE.search(text))


def is_timeout(text: str) -> bool:
    """Whether `text` marks a `pi` call that ran past its time budget without answering — the
    model never served in time, never a bad answer once served. Unlike a 429/503/402, `run_pool`
    treats this as transient rather than definitive: the entry gets one requeue.

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


_MAX_UNTRACKED_BYTES = 200_000
_MAX_FILE_DIFF_CHARS = 200_000
_DIFF_HEADER_RE = re.compile(r"^diff --git a/.+? b/(?P<path>.+)$")


DIFF_SCOPES = ("all", "staged", "unstaged")


def project_dir() -> str:
    """The directory this session works in: `CLAUDE_PROJECT_DIR`, else the current directory."""
    return os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def _git(repo: str, *args: str, check: bool = True) -> str:
    """Stdout of `git -C <repo> <args>`, decoded as UTF-8 with undecodable bytes replaced."""
    return subprocess.run(
        ["git", "-C", repo, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    ).stdout


def repo_root(cwd: str | None = None) -> str:
    """The top-level directory of the repository containing `cwd` (default `project_dir()`).

    Args:
        cwd: a directory inside the repository; defaults to `project_dir()`.

    Returns:
        The repository's top-level directory.

    Raises:
        subprocess.CalledProcessError: `cwd` is not inside a git repository.
        OSError: `git` is not installed.
    """
    return _git(cwd or project_dir(), "rev-parse", "--show-toplevel").strip()


def _cap_file_diffs(diff: str) -> str:
    """`diff` with each file's block cut at `_MAX_FILE_DIFF_CHARS`, a note naming the file in
    place of the cut part — a lockfile or a generated file must not drown the review."""
    blocks = re.split(r"(?m)^(?=diff --git )", diff)
    capped: list[str] = []
    for block in blocks:
        if len(block) > _MAX_FILE_DIFF_CHARS:
            head = _DIFF_HEADER_RE.match(block.splitlines()[0])
            path = head["path"] if head else "?"
            kept = block[:_MAX_FILE_DIFF_CHARS].rpartition("\n")[0]
            block = (
                f"{kept}\n# diff truncated: {path} "
                f"({len(block)} chars > {_MAX_FILE_DIFF_CHARS})\n"
            )
        capped.append(block)
    return "".join(capped)


def diff_against(base: str, cwd: str | None = None, scope: str = "all") -> str:
    """Unified diff of what this work changed since it forked from `base`.

    Covers commits, staged and unstaged edits of tracked files, and untracked files that are not
    git-ignored (a worker never commits, so the change under review is usually still in the
    working tree). The whole repository is diffed, whatever subdirectory `cwd` points into. A
    tracked file's diff past `_MAX_FILE_DIFF_CHARS` is cut there; an untracked file past
    `_MAX_UNTRACKED_BYTES`, or one that is not a regular file (a symlink, a nested repository), is
    replaced by a one-line note. The diff goes to a third-party model:
    an untracked file that is not git-ignored leaves the machine with it.

    `scope` narrows it: `all` (the default) is fork point → working tree; `staged` is fork point →
    index; `unstaged` is index → working tree. Untracked files count as unstaged.

    Args:
        base: the branch or ref the work forked from (`main`); `HEAD` for work not yet committed.
        cwd: a directory inside the repository to diff; defaults to `project_dir()`.
        scope: one of `DIFF_SCOPES`.

    Returns:
        The diff text; empty when nothing changed.

    Raises:
        ValueError: `base` starts with `-` — git would read it as an option — or `scope` is
            not one of `DIFF_SCOPES`.
        subprocess.CalledProcessError: git failed (not a repository, unknown `base`).
        OSError: `git` is not installed.
    """
    if base.startswith("-"):
        raise ValueError(f"base {base!r} looks like an option, not a ref")
    if scope not in DIFF_SCOPES:
        raise ValueError(f"scope {scope!r} is not one of {', '.join(DIFF_SCOPES)}")
    repo = repo_root(cwd)
    fork = _git(repo, "merge-base", base, "HEAD").strip()
    plain = ("diff", "--no-ext-diff", "--no-color")
    tracked = {
        "all": (fork,),
        "staged": ("--cached", fork),
        "unstaged": (),
    }[scope]
    parts = [_cap_file_diffs(_git(repo, *plain, *tracked))]
    if scope == "staged":
        return parts[0]
    untracked = _git(repo, "ls-files", "-z", "--others", "--exclude-standard")
    for path in filter(None, untracked.split("\0")):
        info = (Path(repo) / path).lstat()
        if not stat.S_ISREG(info.st_mode):
            parts.append(f"# untracked entry skipped: {path} (not a regular file)\n")
            continue
        if info.st_size > _MAX_UNTRACKED_BYTES:
            parts.append(
                f"# untracked file skipped: {path} "
                f"({info.st_size} bytes > {_MAX_UNTRACKED_BYTES})\n"
            )
            continue
        # `--no-index` exits 1 when the two sides differ — a new file always does.
        parts.append(
            _git(repo, *plain, "--no-index", "--", os.devnull, path, check=False)
        )
    return "".join(parts)


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
        *(["--thinking", entry["thinking"]] if entry.get("thinking") else []),
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


def build_pool_log_line(
    when: datetime, name: str, kind: str, fields: dict[str, object] | None = None
) -> str:
    """One `run_pool` debug log line: an ISO-8601 UTC timestamp, the entry name, the event kind,
    then ` key=value` for each of `fields`, in order.

    Args:
        when: the event's timestamp — passed in rather than read internally, so a caller can pin
            it and assert the line's exact text.
        name: the pool entry's `name`, or `"(pool)"` for a pool-level event.
        kind: a short label for what happened (`"answered"`, `"empty answer"`, `"timeout"`,
            `"start"`, `"exhausted"`, ...).
        fields: the call's context — model id, prompt size, timeout, attempt, tools, duration.

    Returns:
        The line, newline-terminated.
    """
    detail = "".join(f" {k}={v}" for k, v in (fields or {}).items())
    return f"{when.isoformat()} {name} {kind}{detail}\n"


def _pool_log_path() -> Path:
    """This session's `run_pool` debug log path — keyed by `CLAUDE_CODE_SESSION_ID`.

    `standalone` when unset — a caller outside a Claude Code session still gets one shared file
    rather than no log at all. Shares the env var and the `standalone` fallback string with
    `model_offline`'s own session naming, not its id truncation or its `/tmp` location: this file
    lives under `~/.claude/debug/`, the same durable directory Claude Code already uses for its own
    per-session debug output, keyed by the full untruncated session id.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "standalone")
    return (
        Path.home() / ".claude" / "debug" / f"{session_id}_mcp__pc-core__model-pool.txt"
    )


def log_pool_event(name: str, kind: str, **fields: object) -> None:
    """Best-effort append of one `build_pool_log_line` line to `run_pool`'s debug log.

    Swallows any failure to write it silently (missing `~/.claude/debug/` and unable to create it,
    permission issue, full disk, an unresolvable home directory) — a logging failure must never
    break a `run_pool` call that otherwise succeeded.

    Args:
        name: the pool entry's `name`, or `"(pool)"` for a pool-level event.
        kind: a short label for what happened.
        **fields: the context appended to the line, in keyword order.
    """
    line = build_pool_log_line(datetime.now(timezone.utc), name, kind, fields)
    try:
        path = _pool_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except (OSError, RuntimeError):
        pass


def call_context(
    entry: dict[str, Any],
    prompt: str,
    timeout: int,
    tools: list[str] | None,
    attempt: int,
) -> dict[str, object]:
    """What a `run_pool` log line says about one call — enough to tell, afterwards, why an entry
    answered in one run and not in the next: the exact model id, how big the prompt was, how long
    it was given, which attempt this is, and whether the model had tools.
    """
    return {
        "model": cli_model_id(entry),
        "prompt_chars": len(prompt),
        "timeout": f"{timeout}s",
        "attempt": attempt,
        "tools": ",".join(tools) if tools else "none",
    }


_LOGGED_ERROR_CHARS = 200


def run_pool(
    entries: list[dict[str, Any]],
    prompt: str,
    timeout: int = RUN_TIMEOUT,
    tools: list[str] | None = None,
) -> PoolResult:
    """Try each queued entry in order, first non-empty success wins.

    Returns (entry, answer) on success, or (None, last_error) once the queue is exhausted. A
    `kind: "offline"` entry is never dispatched here — it must go through the separate `offline`
    MCP tool, which enforces its own zero-egress guarantee (never returning the answer body).
    Shared by the CLI (`main`), the MCP tool and `pi_review`, so a fallback fix lands once.

    `is_rate_limited` / `is_out_of_memory` / `is_insufficient_credit` mean the provider refuses
    to serve for the rest of this run: the entry is dropped for good, never requeued. A 402 or an
    organization-wide 429 (`is_provider_wide_limit`) is the provider's own, so its other queued
    entries are skipped too; a 429 naming one model skips only that entry. An empty
    answer or a timeout `pi` itself reports (`is_timeout`) is transient: the entry is appended to
    the end of the remaining queue once; a second such failure from the same entry drops it for
    good — at most `2 * len(entries)` attempts, never an infinite loop. `timeout` is the wall
    clock of the whole `pi` run, tool loop included — `pi` bounds each request itself
    (`retry.provider.timeoutMs`, `httpIdleTimeoutMs`) — so it is a backstop against a run that
    never ends, and a `subprocess.TimeoutExpired` drops the entry without a requeue: a second
    attempt would repeat the whole exploration. Trade-off: a
    pool where every entry times out now takes up to twice as long to exhaust, since each gets a
    second attempt before being dropped — deliberate, in exchange for surviving a transient blip.
    Any other non-zero exit is a terminal stop for the whole pool, not a fall-through.

    Every call, its outcome and the pool's own start and exhaustion are logged through
    `log_pool_event` with the call's context (`call_context`) and its duration, best-effort.
    """
    last_error: str | None = None
    env = secret_env()
    queue: deque[int] = deque(range(len(entries)))
    requeued: set[int] = set()
    attempts: dict[int, int] = {}
    limited_providers: set[str] = set()
    log_pool_event(
        "(pool)",
        "start",
        entries=",".join(e["name"] for e in entries),
        prompt_chars=len(prompt),
    )

    def requeue_once(idx: int) -> None:
        """Append `idx` to the end of the queue on its first transient failure only."""
        if idx not in requeued:
            requeued.add(idx)
            queue.append(idx)

    while queue:
        idx = queue.popleft()
        entry = entries[idx]
        if entry["kind"] == "offline":
            last_error = (
                f"{entry['name']}: offline entries are not callable through the pool"
            )
            log_pool_event(entry["name"], "not callable (offline)")
            continue
        if entry["kind"] != "cli":
            last_error = _not_callable(entry)
            log_pool_event(entry["name"], "not callable")
            continue
        if not entry.get("model"):
            last_error = f"{entry['name']}: cli entries require a 'model' field"
            log_pool_event(entry["name"], "missing model field")
            continue
        if entry.get("provider") in limited_providers:
            log_pool_event(
                entry["name"], "skipped (provider limited)", provider=entry["provider"]
            )
            continue
        attempts[idx] = attempts.get(idx, 0) + 1
        call_timeout = entry_timeout(entry, timeout)
        context = call_context(entry, prompt, call_timeout, tools, attempts[idx])
        started = time.monotonic()
        try:
            answer = call_cli(entry, prompt, call_timeout, tools, env)
        except subprocess.CalledProcessError as e:
            # Les deux flux, pas un seul : un fournisseur peut écrire son code d'erreur sur
            # stdout pendant que stderr porte autre chose.
            combined = f"{e.stdout or ''}\n{e.stderr or ''}"
            seen = {
                **context,
                "elapsed": f"{time.monotonic() - started:.1f}s",
                "error": failure_detail(e)[:_LOGGED_ERROR_CHARS],
            }
            if (
                is_rate_limited(combined)
                or is_out_of_memory(combined)
                or is_insufficient_credit(combined)
            ):
                # Signal définitif : évalué avant `is_timeout` pour qu'un message qui porte les
                # deux marqueurs à la fois (ex. un 429 dont le corps mentionne aussi un timeout
                # amont) reste jamais-requeue plutôt que transitoire.
                last_error = f"{entry['name']}: {combined.strip()}"
                if is_provider_wide_limit(failure_detail(e)):
                    # Solde ou limite d'organisation : ses autres entrées échoueraient pareil.
                    # Un 429 qui nomme un modèle, ou un out-of-memory, ne concerne que lui.
                    limited_providers.add(entry["provider"])
                log_pool_event(
                    entry["name"],
                    "rate limit / out of memory / insufficient credit",
                    **seen,
                )
                continue
            if is_timeout(combined):
                last_error = f"{entry['name']}: {combined.strip()}"
                log_pool_event(entry["name"], "timeout", **seen)
                requeue_once(idx)
                continue
            log_pool_event(entry["name"], "critical failure", **seen)
            return PoolResult(
                None, f"{entry['name']} critical failure: {combined.strip()}"
            )
        except subprocess.TimeoutExpired as e:
            last_error = f"{entry['name']}: {e}"
            log_pool_event(
                entry["name"],
                "run timeout",
                **context,
                elapsed=f"{time.monotonic() - started:.1f}s",
            )
            continue
        except OSError as e:
            last_error = f"{entry['name']}: {e}"
            log_pool_event(
                entry["name"],
                "os error",
                **context,
                elapsed=f"{time.monotonic() - started:.1f}s",
                error=str(e)[:_LOGGED_ERROR_CHARS],
            )
            continue
        else:
            answered = {
                **context,
                "elapsed": f"{time.monotonic() - started:.1f}s",
                "answer_chars": len(answer.strip()),
            }
            if answer.strip():
                log_pool_event(entry["name"], "answered", **answered)
                return PoolResult(entry, answer)
            last_error = f"{entry['name']}: empty answer"
            log_pool_event(entry["name"], "empty answer", **answered)
            requeue_once(idx)
    log_pool_event("(pool)", "exhausted")
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


def filter_pool(
    pool: list[dict[str, Any]],
    provider: str | None = None,
    names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The pool entries matching `provider` and/or `names`, in pool order.

    Args:
        pool: the entries to filter.
        provider: keep only entries of this provider; None keeps every provider.
        names: keep only entries with one of these names; None or empty keeps every name.

    Returns:
        The matching entries — the whole pool when no filter is given.
    """
    return [
        e
        for e in pool
        if (provider is None or e.get("provider") == provider)
        and (not names or e.get("name") in names)
    ]


def filter_miss(provider: str | None, names: list[str] | None) -> str:
    """The report line for a filter that matched no entry, naming what was asked for."""
    asked = []
    if provider is not None:
        asked.append(f"provider '{provider}'")
    if names:
        asked.append("name " + ", ".join(f"'{n}'" for n in names))
    return f"[pool] no entry matches {' and '.join(asked)} — check the spelling against model_pool"


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
    ap.add_argument(
        "--provider",
        help="with --check-pool: only the entries of this provider",
    )
    ap.add_argument(
        "--name",
        action="append",
        default=[],
        dest="names",
        help="with --check-pool: only this pool entry (repeatable)",
    )
    args = ap.parse_args(argv)

    if args.check_pool:
        pool = filter_pool(load_pool(), args.provider, args.names)
        if not pool:
            print(filter_miss(args.provider, args.names), file=sys.stderr)
            return 2
        results = check_pool(pool)
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
