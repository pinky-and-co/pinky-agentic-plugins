#!/usr/bin/env python3
"""
Claude Code status line — 3 lines + 1 conditional warning line (rate limits).
Reads the JSON sent via stdin by Claude Code with every update.

Layout: each line is either a simple concatenation of segments (with a separator
between them) or a "flex" layout where a left group and a right group are
pushed to opposite ends of the line (like `ccstatusline` flex separators),
with a guaranteed minimum gap between them if the terminal is too narrow
to separate them fully.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from _common import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    GREY,
    MAGENTA,
    RED,
    RESET,
    YELLOW,
    enable_ansi_on_windows,
    force_utf8_stdout,
    render_bar,
)

enable_ansi_on_windows()
force_utf8_stdout()

# --- ANSI colors — statusline-only, on top of the shared palette in _common ---

WHITE = "\033[37m"
YELLOW_BG = "\033[43;30m"  # yellow background, black text ->  "warning" effect
RED_BG = "\033[41;30m"  # red background, white text ->  "alert" effect

SEP = f"  {GREY}·{RESET}  "

# Headroom (optional third-party tool, headroomlabs-ai/headroom) proxies requests through a local
# port and serves live counters there. Nothing listens when Headroom is not running — the line then
# never renders. Scope is the proxy's own lifetime, which `headroom wrap claude` ties to one Claude
# Code session.
_HEADROOM_STATS_URL = "http://127.0.0.1:8787/stats"
# A status line repaints on every turn, so the HTTP call is both hard-bounded and memoized: a hung
# proxy costs one timeout, not one per paint.
_HEADROOM_HTTP_TIMEOUT_S = 0.15
_HEADROOM_CACHE_TTL_S = 5.0
_HEADROOM_CACHE = os.path.join(
    tempfile.gettempdir(), f"pc-statusline-headroom-{os.getuid()}.json"
)

# Minimum blank gap between a row's left and right groups, and the gutter added past the widest
# natural row to place the shared right-hand column.
_MIN_GAP = 10
# A braille blank (U+2800): an invisible, non-whitespace glyph. Used two ways below — as the
# spacer row between content rows (the host collapses a row that is empty or whitespace-only, both
# tried), and as row 0's left anchor (a row that starts with a space gets left-trimmed, which
# would drag the version string away from its column).
_BLANK = "⠀"
_SPACER_ROW = _BLANK


def _safe_get(d, path, default=None):
    """Secure access to a nested path like 'a.b.c'."""
    cur = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur if cur is not None else default


def _hyperlink(text: str, url: str) -> str:
    """Wrap `text` in an OSC 8 terminal hyperlink pointing at `url` — supported by most modern
    terminals (iTerm2, kitty, wezterm, the VS Code integrated terminal…), silently inert (renders
    as plain `text`) on ones that don't. `_visible_len` below must stay in sync with this format —
    it treats the whole wrapper as zero-width, same as an SGR color code."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def _visible_len(s: str):
    """Text length excluding ANSI codes, for flex alignment. Two escape kinds appear in this
    file's output: SGR color codes (`ESC [ ... m`) and OSC 8 hyperlinks (`ESC ] ... ST`, `ST` being
    either `ESC \\` or BEL `\\x07` — see `_hyperlink`). Both are zero-width; anything else starting
    with `ESC` is skipped defensively, one character at a time, rather than counted as visible."""
    WIDE_RANGES = (
        (0x1100, 0x115F),
        (0x2300, 0x23FF),
        (0x2600, 0x27BF),
        (0x2E80, 0xA4CF),
        (0xAC00, 0xD7A3),
        (0xF900, 0xFAFF),
        (0xFF00, 0xFF60),
        (0xFFE0, 0xFFE6),
        (0x1F1E6, 0x1F1FF),  # drapeaux
        (0x1F300, 0x1FAFF),  # emoji principaux
    )
    ZERO_WIDTH_CODEPOINTS = {0x200D, 0xFE0E, 0xFE0F}  # ZWJ, VS15, VS16

    out = 0
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\033":
            nxt = s[i + 1] if i + 1 < n else ""
            if nxt == "[":  # SGR: ESC [ ... m
                j = i + 2
                while j < n and s[j] != "m":
                    j += 1
                i = j + 1
            elif nxt == "]":  # OSC 8: ESC ] ... (BEL | ESC \)
                j = i + 2
                while (
                    j < n
                    and s[j] != "\x07"
                    and not (s[j] == "\033" and s[j + 1 : j + 2] == "\\")
                ):
                    j += 1
                i = j + 1 if (j < n and s[j] == "\x07") else j + 2
            else:
                i += 1
            continue

        cp = ord(ch)
        if cp in ZERO_WIDTH_CODEPOINTS or unicodedata.combining(ch):
            i += 1
            continue  # largeur 0 : se colle au caractère précédent
        if any(lo <= cp <= hi for lo, hi in WIDE_RANGES):
            out += 2
        else:
            out += 1
        i += 1
    return out


def _render_flex(
    left_parts: list[str], right_parts: list[str], right_edge: int, min_gap=_MIN_GAP
):
    """Join `left_parts` and `right_parts` each with `SEP`, then pad so the right group *ends* at
    column `right_edge` — every row's right group lines up on the same right-hand column. When the
    two groups are too wide to keep `min_gap` between them, the gap floors at `min_gap` and the
    row overflows `right_edge` rather than the groups overlapping.

    `right_edge` is a content-derived width (the widest natural row), never the terminal edge — a
    width read from COLUMNS goes stale the moment a VSCode panel is resized. A shorter right group
    just gets more padding, so alignment holds even on the row whose left side is empty."""
    left = SEP.join(left_parts)
    if not right_parts:
        return left
    right = SEP.join(right_parts)
    pad = max(min_gap, right_edge - _visible_len(left) - _visible_len(right))
    return f"{left}{' ' * pad}{right}"


def _content_widths(flex_specs):
    """`(max_left, max_right)` in visible columns across `flex_specs` — the width of the widest
    left group and of the widest right group, measured separately so the right column can be
    placed at one consistent position for every row."""
    left_widths = [_visible_len(SEP.join(lp)) for lp, _ in flex_specs]
    right_widths = [_visible_len(SEP.join(rp)) for _, rp in flex_specs if rp]
    return (max(left_widths, default=0), max(right_widths, default=0))


def _color_for_pct(pct):
    if pct >= 75:
        return RED
    if pct >= 50:
        return YELLOW
    return GREEN


def _color_for_hit_rate(pct, warmed_up: bool):
    "Inverted logic: here, a LOW rate is the problem (more fresh tokens = higher cost), unlike with the context window."
    if warmed_up:
        if pct < 30:
            return "🔴 " + RED
        if pct < 60:
            return "🟡 " + YELLOW
        return "🟢 " + GREEN
    else:
        return "⚪️ " + DIM


def _build_bar(pct, width=10, filled_char="█", empty_char="░"):
    return render_bar(
        pct,
        width=width,
        color=_color_for_pct(pct),
        filled=filled_char,
        empty=empty_char,
    )


def _fmt_epoch(epoch_seconds):
    """Displays only the time if the reset is today; otherwise, displays date + time
    (useful for the '7-day' reset, which often falls on a different day)."""
    try:
        dt = datetime.fromtimestamp(epoch_seconds)
        now = datetime.now()
        if dt.date() == now.date():
            return dt.strftime("%H:%M")
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return "?"


def _count_subagents(transcript_path, session_id):
    """The sub-agents (Task tool / context:fork) each write their own
    file to <transcript-folder>/<session_id>/subagents/agent-*.jsonl
    — a sibling folder to the main .jsonl file, not inside it. We simply
    count the existing files without parsing them (negligible cost,
    called on every refresh)."""
    if not transcript_path or not session_id:
        return None
    try:
        parent_dir = os.path.dirname(transcript_path)
        subagents_dir = os.path.join(parent_dir, session_id, "subagents")
        if not os.path.isdir(subagents_dir):
            return 0
        return len(
            [
                f
                for f in os.listdir(subagents_dir)
                if f.startswith("agent-") and f.endswith(".jsonl")
            ]
        )
    except Exception:
        return None


def _get_advisor_model(transcript_path, tail_bytes=16384):
    """advisorModel is not exposed in the status line's stdin JSON (it is not in the documented schema),
    but it appears in every assistant entry in the .jsonl file when --advisor is active.
    Only the end of the file (tail_bytes) is read to avoid re-parsing a potentially huge transcript on every refresh.."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            chunk = f.read().decode("utf-8", errors="ignore")

        # Coupe la première ligne, probablement partielle (le seek tombe
        # souvent au milieu d'une ligne JSON).
        lines = chunk.split("\n")[1:]

        for raw_line in reversed(lines):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "assistant" and "advisorModel" in entry:
                return entry["advisorModel"]
        return None
    except Exception:
        return None


def _headroom_stats() -> dict | None:
    """Raw Headroom proxy counters, memoized on disk for `_HEADROOM_CACHE_TTL_S`.

    Returns the decoded `/stats` payload, or None when the proxy is down, slow or unreadable —
    every failure mode is the same answer, because a status line has nothing useful to say about
    a tool that is not running.
    """
    try:
        cached = os.path.getmtime(_HEADROOM_CACHE)
        if (datetime.now().timestamp() - cached) < _HEADROOM_CACHE_TTL_S:
            with open(_HEADROOM_CACHE, encoding="utf-8") as handle:
                return json.load(handle)
    except (OSError, ValueError):
        pass  # no cache yet, or a torn write from a concurrent paint — refetch

    try:
        with urllib.request.urlopen(  # nosec B310 — the URL is the module constant above, a hardcoded http:// loopback address; no caller input reaches it, so no file:/ or custom scheme is reachable
            _HEADROOM_STATS_URL, timeout=_HEADROOM_HTTP_TIMEOUT_S
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None

    try:
        with open(_HEADROOM_CACHE, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except OSError:
        pass  # an unwritable temp dir costs a refetch per paint, never a missing line
    return payload


def _headroom_net(stats: dict | None = None) -> tuple[int, float, int] | None:
    """Net effect of Headroom's compression, as `(net_tokens, percent, bust_count)`.

    Compression removes tokens before send, but rewriting a request mutates the cached prefix and
    invalidates it: the tokens that would have been read at a discount are re-sent instead. Only
    the difference says whether the tool is paying for itself, so the gross figure Headroom
    advertises is deliberately not what this reports.

    Tool-schema deferral is excluded on purpose. Routing through the proxy is what disables
    on-demand tool loading in the first place (Headroom issue #746), so re-enabling it restores the
    unproxied baseline rather than beating it — counting it as a gain would overstate the tool the
    same way the gross figure does.

    `percent` is the net against what the session would have sent uncompressed, so it reads as
    "N% cheaper" or "N% dearer". Its denominator counts only the requests actually compressed while
    busts are charged against the whole session, so a negative reading errs pessimistic rather than
    flattering the tool. It is always positive; `net_tokens` carries the sign.
    """
    stats = _headroom_stats() if stats is None else stats
    if not isinstance(stats, dict):
        return None

    saved = (
        _safe_get(
            stats, "prefix_cache.compression_vs_cache.tokens_saved_by_compression"
        )
        or 0
    )
    lost = (
        _safe_get(stats, "prefix_cache.compression_vs_cache.tokens_lost_to_cache_bust")
        or 0
    )
    before = _safe_get(stats, "summary.compression.total_tokens_before") or 0
    busts = _safe_get(stats, "prefix_cache.compression_vs_cache.cache_bust_count") or 0
    if not isinstance(saved, int) or not isinstance(lost, int) or not before:
        return None

    net = saved - lost
    if net == 0:
        return None
    return (net, abs(net) / before * 100, busts if isinstance(busts, int) else 0)


def _fmt_headroom_line(net: int, percent: float, busts: int) -> str:
    """The Headroom row: one verdict, one magnitude, coloured by the sign.

    Deliberately not a neutral indicator — a reader who has never heard of prompt caching must be
    able to tell in one glance whether the tool is helping, so the wording flips with the sign
    instead of staying constant and leaving the reader to work out what a minus means.
    """
    amount = abs(net)
    amount_txt = f"{amount // 1000}k" if amount >= 1000 else str(amount)
    pct_txt = f"{percent:.1f}%" if percent < 10 else f"{percent:.0f}%"
    if net > 0:
        colour, face, verb, name = GREEN, "🤑", "saved", "savings"
    else:
        colour, face, verb, name = RED, "💸", "lost", "losses"
    bust_txt = f"{SEP}{busts} cache bust{'s' if busts != 1 else ''}" if busts else ""
    return f"Headroom {face}{SEP}{colour}{pct_txt} {name}{SEP}{colour}{amount_txt} tokens {verb}{RESET}{bust_txt}"


_GIT_TIMEOUT_S = 0.3


def _repo_branch(start_dir):
    """`"<basename(repo-root)> @ <branch>"` for the git repo `start_dir` sits in (or a subdir of),
    or `None` outside a git repo / on any lookup failure: timeout, no git binary, detached-HEAD
    edge cases. `CLAUDE_PROJECT_DIR` is often empty in Claude Code and `${CLAUDE_PROJECT_DIR}`-style
    prompts don't render, so this is the one place a session can see at a glance which repo/branch
    it is actually sitting in. Untruncated — it has its own row, nothing else to crowd.

    Shells out to `git -C <dir> ...` (`rev-parse --show-toplevel` + `symbolic-ref --short HEAD`) —
    the same idiom `pc-git`'s own hooks already use, rather than hand-parsing `.git/HEAD` (worktrees,
    packed-refs and detached HEAD are git's own problem to solve correctly, not this script's)."""
    if not start_dir:
        return None
    try:
        root = subprocess.run(
            ["git", "-C", start_dir, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        if root.returncode != 0:
            return None
        repo_root = root.stdout.strip()
        branch = subprocess.run(
            ["git", "-C", start_dir, "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        branch_name = branch.stdout.strip() if branch.returncode == 0 else "detached"
        return f"{os.path.basename(repo_root)} @ {branch_name}"
    except Exception:
        return None


_FORGE_REMOTE_RE = re.compile(r"^(?:git@|https://|http://)([^:/]+)[:/](.+?)(?:\.git)?$")


def _forge_repo_slug(remote_url: str):
    """`(hostname, "org/repo")` from a git remote URL, SSH or HTTPS form — or `None` if the URL
    doesn't match either shape."""
    m = _FORGE_REMOTE_RE.match(remote_url.strip())
    return (m.group(1), m.group(2)) if m else None


def _repo_url(start_dir):
    """URL of the current branch's page on GitHub/GitLab for the repo `start_dir` sits in, or the
    repo's root page when the branch has no upstream yet (a fresh local branch has no remote tree
    to link to — the branch-specific URL would 404), or `None` outside a repo / no `origin` remote
    / an unrecognised forge / any lookup failure. Matched by "github"/"gitlab" appearing in the
    remote's hostname, which covers both the public services and the common self-hosted case."""
    if not start_dir:
        return None
    try:
        remote = subprocess.run(
            ["git", "-C", start_dir, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        if remote.returncode != 0:
            return None
        slug = _forge_repo_slug(remote.stdout)
        if not slug:
            return None
        host, path = slug
        if "github" in host:
            base, tree_sep = f"https://{host}/{path}", "tree"
        elif "gitlab" in host:
            base, tree_sep = f"https://{host}/{path}", "-/tree"
        else:
            return None

        upstream = subprocess.run(
            [
                "git",
                "-C",
                start_dir,
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{u}",
            ],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        if upstream.returncode != 0:
            return base  # no upstream — the branch tree page would 404, link the repo instead
        branch = subprocess.run(
            ["git", "-C", start_dir, "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        if branch.returncode != 0:
            return base
        return f"{base}/{tree_sep}/{branch.stdout.strip()}"
    except Exception:
        return None


def _short_model(model_id):
    """claude-opus-4-8 -> Opus 4.8, claude-sonnet-5 -> Sonnet 5, etc."""
    if not model_id:
        return None
    name = model_id.replace("claude-", "")
    parts = name.split("-")
    if not parts:
        return model_id
    label = parts[0].capitalize()
    version_parts = [p for p in parts[1:] if not (p.isdigit() and len(p) >= 6)]
    version = ".".join(version_parts)
    return f"{label} {version}".strip()


def build_lines(data: dict) -> list[str]:
    """Render the status line as a list of lines from the harness payload.

    Pure + total in intent: `main()` wraps this call in a try/except so a malformed payload
    degrades to a bare fallback line instead of blanking the whole bar — but this function itself
    stays the single, testable entry point (tests.md § generator oracle)."""
    transcript_path = _safe_get(data, "transcript_path")
    lines: list[str] = []

    version = _safe_get(data, "version", "?")
    # Row 0 carries only the version on the right; its left is an invisible anchor (see _BLANK),
    # replaced by the rate-limit banner below when one fires.
    line0_left_parts = [_BLANK]
    line0_right_parts = [f"{DIM}v{version}{RESET}"]
    # ---------- Ligne 1 : version, modèle, effort, thinking, fast mode, output style ----------
    model = _safe_get(data, "model.display_name", "?")
    effort = _safe_get(data, "effort.level")
    thinking_on = _safe_get(data, "thinking.enabled", False)
    fast_on = _safe_get(data, "fast_mode", False)
    style = _safe_get(data, "output_style.name", "default")

    effort_txt = f"💪 {effort}" if effort else f"{DIM}💪 —{RESET}"
    thinking_txt = "🧠 ON" if thinking_on else f"{DIM}🧠 off{RESET}"
    fast_txt = "⚡ fast" if fast_on else f"{DIM}🐢 normal{RESET}"

    advisor_model = _short_model(_get_advisor_model(transcript_path))

    line1_left_parts: list[str] = [
        f"{CYAN}{BOLD}🤖 {model}{RESET}",
        f"{effort_txt}",
        f"{fast_txt}",
    ]

    if advisor_model:
        line1_left_parts.append(f"{YELLOW}🧐 {advisor_model}{RESET}")

    line1_right_parts: list[str] = [
        f"{thinking_txt}",
        f"🎨 {style}{RESET}",
    ]

    # ---------- Ligne 2 : session id (courte, en lien vers le transcript), heure de début,
    # nombre de subagents à droite ----------
    session_id = _safe_get(data, "session_id", "?")
    short_session_id = (
        session_id[:8] if session_id and session_id != "?" else session_id
    )
    # Court pour la lecture ; le lien porte l'id complet pour un outil de recherche par session
    # (ex. le transcript lui-même) — le texte affiché n'a plus besoin d'être long pour ça.
    session_txt = (
        _hyperlink(short_session_id, f"file://{transcript_path}")
        if transcript_path
        else short_session_id
    )

    duration_ms = _safe_get(data, "cost.total_duration_ms", 0) or 0
    start_dt = datetime.now() - timedelta(milliseconds=duration_ms)
    start_txt = start_dt.strftime("%d/%m %H:%M")
    subagent_count = _count_subagents(transcript_path, session_id)

    line2_left_parts: list[str] = [
        f"{MAGENTA}Session 🆔 {session_txt}{RESET}",
        f"🕐 Started at {start_txt}{RESET}",
    ]
    line2_right_parts: list[str] = (
        [
            f"{CYAN}🧬 {subagent_count} subagent{'s' if subagent_count > 1 else ''}{RESET}"
        ]
        if subagent_count
        else []
    )

    # ---------- Ligne repo/branche (entre session et context window), en lien vers le repo —
    # CLAUDE_PROJECT_DIR est souvent vide dans Claude Code, c'est le seul endroit où une session
    # voit dans quel repo/branche elle travaille réellement. Ligne omise hors d'un repo. ----------
    repo_dir = _safe_get(data, "workspace.current_dir") or _safe_get(data, "cwd")
    repo_branch = _repo_branch(repo_dir)
    repo_line_left_parts: list[str] = []
    if repo_branch:
        repo_url = _repo_url(repo_dir)
        repo_txt = f"📁 {repo_branch}"
        repo_line_left_parts.append(
            _hyperlink(repo_txt, repo_url) if repo_url else repo_txt
        )
    repo_line_right_parts: list[str] = []

    # ---------- Line 3 : context bar + hit cache performance ----------
    pct = _safe_get(data, "context_window.used_percentage", 0) or 0
    pct = int(pct)
    bar = _build_bar(pct)
    cost = _safe_get(data, "cost.total_cost_usd", 0) or 0
    total_input_tokens = _safe_get(data, "context_window.total_input_tokens", 0) or 0

    usage = _safe_get(data, "context_window.current_usage")
    if usage:
        cache_c = usage.get("cache_creation_input_tokens", 0) or 0
        cache_r = usage.get("cache_read_input_tokens", 0) or 0
        hit_rate = (cache_r / total_input_tokens * 100) if total_input_tokens > 0 else 0
        # At the very beginning of a session (nothing to reuse yet, cache not
        # yet primed), a low rate is normal: no alert.
        # The cache only becomes a meaningful signal once data has been
        # written to the cache (cache_c > 0) for reuse.
        warmed_up = cache_c > 0 or cache_r > 0
        color = _color_for_hit_rate(hit_rate, warmed_up)
        cache_txt = f"{color}{hit_rate:.0f}%{RESET}"
    else:
        cache_txt = f"{DIM}—{RESET}"

    line3_left_parts: list[str] = [
        f"Context window {bar} {_color_for_pct(pct)}{pct}%{RESET}",
        f"Cache hit {cache_txt}{RESET}",
    ]
    line3_right_parts: list[str] = [f"💲 {cost:.2f} (API-equiv)"]

    # ---------- Optional line : Headroom, only while its proxy is up ----------
    line4_left_parts: list[str] = []
    line4_right_parts: list[str] = []
    headroom = _headroom_net()
    if headroom:
        line4_left_parts.append(_fmt_headroom_line(*headroom))

    # ---------- Optional line : rate limits > 90% ----------
    warnings = []
    bg = YELLOW_BG
    for label, key in (("Session", "five_hour"), ("Weekly", "seven_day")):
        used = _safe_get(data, f"rate_limits.{key}.used_percentage")
        resets_at = _safe_get(data, f"rate_limits.{key}.resets_at")
        if used is not None and used > 90:
            reset_txt = _fmt_epoch(resets_at) if resets_at else "?"
            warnings.append(f"{label}: {used:.0f}% (reset {reset_txt})")
            if used >= 100:
                bg = RED_BG

    if warnings:
        line0_left_parts = [f"{bg} ⚠ RATE LIMIT — {' | '.join(warnings)} {RESET}"]

    max_left, max_right = _content_widths(
        [
            (line1_left_parts, line1_right_parts),
            (line2_left_parts, line2_right_parts),
            (repo_line_left_parts, repo_line_right_parts),
            (line3_left_parts, line3_right_parts),
            (line4_left_parts, line4_right_parts),
        ]
    )
    # Shared right-hand column: the widest natural row plus one gutter. Content-derived on
    # purpose - a width read from COLUMNS goes stale the moment a VSCode panel is resized.
    right_edge = max_left + max_right + _MIN_GAP

    lines.append(_render_flex(line0_left_parts, line0_right_parts, right_edge))
    lines.append(_SPACER_ROW)
    lines.append(_render_flex(line1_left_parts, line1_right_parts, right_edge))
    lines.append(_SPACER_ROW)
    lines.append(_render_flex(line2_left_parts, line2_right_parts, right_edge))
    lines.append(_SPACER_ROW)
    if repo_line_left_parts:
        lines.append(
            _render_flex(repo_line_left_parts, repo_line_right_parts, right_edge)
        )
        lines.append(_SPACER_ROW)
    lines.append(_render_flex(line3_left_parts, line3_right_parts, right_edge))
    lines.append(_SPACER_ROW)
    if line4_left_parts:
        lines.append(_render_flex(line4_left_parts, line4_right_parts, right_edge))
        lines.append(_SPACER_ROW)

    return lines


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        print("[status line: invalid JSON]")
        return
    try:
        print("\n".join(build_lines(data)))
    except Exception:
        # A statusline must never crash — a bare fallback is better than an error blanking the bar.
        print("⬡")


if __name__ == "__main__":
    main()
