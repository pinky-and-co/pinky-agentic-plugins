#!/usr/bin/env python3
"""
Claude Code status line — 5 rows (budget, cost, repo, session, model) + conditional rows
(rate-limit alert above, compression proxy below).
Reads the JSON sent via stdin by Claude Code with every update.

Layout: three logical rows — budget + cost, repo + session, model. A logical row with both a left and
a right group is fitted as if the two shared one line (left + gap + right must fit the terminal
width), then drawn on two: the right group first, right-aligned, then the left group, so the right
text never sits under the left text of the line next to it. Two layouts share the same rows: the
long one (full labels) when every logical row fits, otherwise the short one (labels dropped,
tighter separator, branch name truncated, model row reduced to the essentials). The width is read from `COLUMNS` once per refresh and
handed to `build_lines` as `_term` by `main()`.
"""

import fcntl
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import termios
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

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
    dump_debug_payload,
    enable_ansi_on_windows,
    force_utf8_stdout,
    process_flags,
    render_bar,
    write_json_atomic,
)
from _quota import provider_rate_limits

enable_ansi_on_windows()
force_utf8_stdout()

SEP = f"  {GREY}·{RESET}  "
_SEP_SHORT = f" {GREY}·{RESET} "

_HEADROOM_STATS_URL = "http://127.0.0.1:8787/stats"
_HEADROOM_HTTP_TIMEOUT_S = 0.15
_HEADROOM_CACHE_TTL_S = 5.0
_HEADROOM_CACHE = os.path.join(
    tempfile.gettempdir(), f"pc-statusline-headroom-{os.getuid()}.json"
)

# Terminal width assumed when `_term` is absent from the payload (tests, `_term_cols` failing).
_DEFAULT_COLS = 120
# Columns Claude Code takes out of the terminal width for a statusline row: it indents every row by
# 2 and truncates anything reaching the last 2 columns with an `…` (measured on a 158-column
# terminal: 154 usable).
_RIGHT_MARGIN = 4
# Minimum gap between a row's left and right groups, long layout / short layout.
_MIN_GAP = 4
_MIN_GAP_SHORT = 2
# Shortest a branch name is allowed to get when the short layout truncates it.
_MIN_BRANCH_COLS = 8
# Claude Code trims the leading whitespace of every statusline row, which would pull a row that has
# only a right group back to the left edge. A braille blank (U+2800) is invisible but not
# whitespace: as the left group of such a row it keeps the padding, and so the right alignment.
_BLANK = "\u2800"

# Set to a truthy value, Claude Code turns the advisor tool off — the flag then has no effect.
_ADVISOR_DISABLED_ENV = "CLAUDE_CODE_DISABLE_ADVISOR_TOOL"
# A transcript line is only parsed when it carries this: the harness writes one `attachment` of
# this type at every advisor add or removal.
_ADVISOR_ENTRY_MARK = b"advisor_tool"


def _term_cols(default=_DEFAULT_COLS):
    try:
        cols = int(os.environ["COLUMNS"])
        if cols > 0:
            return cols
    except (KeyError, ValueError):
        pass
    try:
        tty = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(os.getppid())],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        fd = os.open(f"/dev/{tty}", os.O_RDONLY | os.O_NOCTTY)
        try:
            return (
                struct.unpack("hhhh", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[1]
                or default
            )
        finally:
            os.close(fd)
    except Exception:
        return default


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
        (0x1F194, 0x1F194),
        (0x1F1E6, 0x1F1FF),
        (0x1F300, 0x1FAFF),
    )
    ZERO_WIDTH_CODEPOINTS = {0x200D, 0xFE0E, 0xFE0F}

    out = 0
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\033":
            nxt = s[i + 1] if i + 1 < n else ""
            if nxt == "[":
                j = i + 2
                while j < n and s[j] != "m":
                    j += 1
                i = j + 1
            elif nxt == "]":
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
            continue
        if any(lo <= cp <= hi for lo, hi in WIDE_RANGES):
            out += 2
        else:
            out += 1
        i += 1
    return out


def _render_flex(
    left_parts: list[str],
    right_parts: list[str],
    right_edge: int,
    sep: str = SEP,
    min_gap: int = _MIN_GAP,
):
    """Join `left_parts` and `right_parts` each with `sep`, then pad so the right group *ends* at
    column `right_edge` — the usable terminal width, i.e. `COLUMNS` minus `_RIGHT_MARGIN`. Every
    right group therefore lines up on the same right-hand column.

    `COLUMNS` is read afresh on each refresh by `_term_cols` (in cmux it follows a pane resize);
    `build_lines` receives it as `_term`. When the two groups can't keep `min_gap` between them the
    gap floors at `min_gap` and the row overflows `right_edge` rather than the groups overlapping —
    `build_lines` switches to the short layout before that happens, so an overflow means even the
    short layout does not fit this width."""
    left = sep.join(left_parts)
    if not right_parts:
        return left
    right = sep.join(right_parts)
    pad = max(min_gap, right_edge - _visible_len(left) - _visible_len(right))
    return f"{left}{' ' * pad}{right}"


def _row_width(left_parts, right_parts, sep, min_gap):
    """Width a `(left, right)` pair needs at its tightest: both groups side by side with `min_gap`
    between them, or the one group that is there. This is the figure the layout is chosen on, even
    when `_render_pair` then draws the two groups on separate lines."""
    left = _visible_len(sep.join(left_parts)) if left_parts else 0
    right = _visible_len(sep.join(right_parts)) if right_parts else 0
    return left + right + (min_gap if left and right else 0)


def _render_pair(left_parts, right_parts, right_edge, sep=SEP):
    """A `(left, right)` pair on two lines: the right group first, right-aligned on `right_edge`,
    then the left group. A group that is empty simply has no line. The right-aligned line starts
    with a `_BLANK` standing in for one of its padding cells, so the padding survives Claude Code's
    trimming without costing a column."""
    lines = []
    if right_parts:
        right = sep.join(right_parts)
        pad = right_edge - _visible_len(right)
        lines.append(f"{_BLANK}{' ' * (pad - 1)}{right}" if pad >= 1 else right)
    if left_parts:
        lines.append(sep.join(left_parts))
    return lines


def _truncate_end(text: str, max_len: int) -> str:
    """`text` cut to `max_len` characters, the last one an `…` when something was dropped."""
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 1)] + "…"


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


def _fmt_duration_until(epoch_seconds):
    """Countdown to `epoch_seconds` from now — `"27 min"`, `"1h 5min"`, `"1h"`, then days from 24h
    up (`"1d 3h"`, `"2d"`; minutes are dropped at that scale). `"now"` once the deadline has passed
    (a stale payload should never show a negative duration), `"?"` on a garbage input — same
    degrade-to-`"?"` contract as `_fmt_epoch`, the sibling it is always shown alongside."""
    try:
        target = datetime.fromtimestamp(epoch_seconds)
        total_minutes = int((target - datetime.now()).total_seconds() // 60)
        if total_minutes <= 0:
            return "now"
        hours, minutes = divmod(total_minutes, 60)
        if hours >= 24:
            days, hours = divmod(hours, 24)
            return f"{days}d {hours}h" if hours else f"{days}d"
        if hours > 0:
            return f"{hours}h {minutes}min" if minutes else f"{hours}h"
        return f"{minutes} min"
    except Exception:
        return "?"


_RATE_LIMIT_WINDOW_S = {"five_hour": 5 * 3600, "seven_day": 7 * 24 * 3600}
_RATE_LIMIT_LABEL = {"five_hour": "5h", "seven_day": "7d"}
# Floor per window below which nothing is shown: the 5h window burns fast, so it warns earlier;
# on the weekly one, 90% still leaves a lot of room.
_RATE_LIMIT_FLOOR_PCT = {"five_hour": 75, "seven_day": 90}
# With less than this before the reset, the wall is not worth a warning: the window is about to
# renew anyway.
_RATE_LIMIT_MIN_RESET_S = 3600
# From this percentage up, the projected wall is hidden: `used_percentage` is an integer, and near
# 100% one point halves the projection `elapsed * (100 - u) / u` (0 at 100). Such a figure would
# claim a precision the payload lacks, so only the observed state is shown.
_RATE_LIMIT_PROJECTION_CEILING_PCT = 98


def _rate_limit_wall_seconds(used_pct, resets_at, window_s, now=None):
    """Seconds until the quota runs out at the window's average pace so far — or `None` when the
    quota will last until the reset, when the reset is under `_RATE_LIMIT_MIN_RESET_S` away, or
    on a payload that can't be projected (nothing consumed, `resets_at` further out than the
    window is long).

    The wall comes before the reset exactly when `used_fraction > elapsed_fraction` (linear pace
    assumption): that predicate is the whole test, and the returned value is `elapsed * (100 -
    used) / used`. The average pace is pessimistic after a quiet stretch — it warns early rather
    than late."""
    if now is None:
        now = datetime.now().timestamp()
    if not used_pct or used_pct <= 0 or not resets_at:
        return None
    until_reset = resets_at - now
    elapsed = window_s - until_reset
    if until_reset <= _RATE_LIMIT_MIN_RESET_S or elapsed <= 0:
        return None
    if used_pct / 100 <= elapsed / window_s:
        return None
    return elapsed * (100 - used_pct) / used_pct


def _rate_limit_detail(key, used, resets_at, short=False):
    """The text that follows the bar and the percentage in one window's alert, or `None` to stay
    silent.

    Silent below the window's floor (`_RATE_LIMIT_FLOOR_PCT`). At 100% or with no `resets_at`,
    the percentage is all there is to show. From `_RATE_LIMIT_PROJECTION_CEILING_PCT` up the
    projection is too unstable to print, so the observed state is shown (still silent when the
    reset is imminent). Below it, an alert only appears when the projection says the quota won't
    last until the reset, expressed as time rather than a percentage.

    `short` drops the reset time where a projected wall exists (`limit in ~1h 5min` is the point of
    the alert); the states with no wall to show fall back to the time left before the reset."""
    if used is None or used < _RATE_LIMIT_FLOOR_PCT[key]:
        return None
    if not resets_at:
        return "(reset ?)"
    now = datetime.now().timestamp()
    if used >= 100 or used >= _RATE_LIMIT_PROJECTION_CEILING_PCT:
        if used < 100 and resets_at - now <= _RATE_LIMIT_MIN_RESET_S:
            return None
        if short:
            return f"reset in {_fmt_duration_until(resets_at)}"
        return f"(in {_fmt_duration_until(resets_at)}, reset {_fmt_epoch(resets_at)})"
    wall = _rate_limit_wall_seconds(used, resets_at, _RATE_LIMIT_WINDOW_S[key], now)
    if wall is None:
        return None
    wall_txt = _fmt_duration_until(now + wall)
    wall_txt = "<1 min" if wall_txt == "now" else f"~{wall_txt}"
    if short:
        return f"limit in {wall_txt}"
    return (
        f"limit in {wall_txt} "
        f"(reset in {_fmt_duration_until(resets_at)}, {_fmt_epoch(resets_at)})"
    )


def _rate_limit_alert(key, data, short=False):
    """One window's alert segment — `5h ━━━━━━━━── 82% limit in ~1h 5min (...)` — or `None` while
    the window has nothing to warn about. Coloured yellow, red once the window is exhausted."""
    used = _safe_get(data, f"rate_limits.{key}.used_percentage")
    resets_at = _safe_get(data, f"rate_limits.{key}.resets_at")
    detail = _rate_limit_detail(key, used, resets_at, short)
    if detail is None:
        return None
    color = RED if used >= 100 else YELLOW
    bar = render_bar(
        max(0, min(100, used)), width=10, color=color, filled="━", empty="─"
    )
    return f"{color}{_RATE_LIMIT_LABEL[key]}{RESET} {bar} {color}{used:.0f}% {detail}{RESET}"


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


def _advisor_entry(line: bytes) -> dict | None:
    """The advisor state an `advisor_tool` attachment line carries — `{"available": bool,
    "model": str | None}` — or None for any other line (an assistant message that merely quotes
    the word, malformed JSON, an entry with no usable `available`/`model`)."""
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "attachment":
        return None
    attachment = obj.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "advisor_tool":
        return None
    available, model = attachment.get("available"), attachment.get("model")
    if available is False:
        return {"available": False, "model": None}
    if available is True and isinstance(model, str) and model:
        return {"available": True, "model": model}
    return None


def _scan_advisor_entries(path: str, offset: int) -> tuple[dict | None, int]:
    """The last advisor entry written to the transcript `path` after byte `offset`, and the
    offset the next read resumes from.

    Only complete lines move the offset: a last line with no newline yet may be half written, so it
    is parsed (a finished entry is used) but read again next time."""
    entry = None
    resume = offset
    with open(path, "rb") as handle:
        handle.seek(offset)
        for line in handle:
            if _ADVISOR_ENTRY_MARK in line:
                entry = _advisor_entry(line) or entry
            if not line.endswith(b"\n"):
                break
            resume += len(line)
    return entry, resume


def _transcript_advisor(session_id: str, transcript_path: str | None) -> dict | None:
    """The last advisor entry the session's transcript carries, or None when none was ever seen.

    The transcript is append-only and the entry may date from the start of the session, so the
    last entry read and the byte offset already scanned are kept per session in
    `statusline-advisor-transcript-<session_id>.json`; a render reads only what was appended since.
    The entry is never dropped on a failed read: a missing or unreadable transcript leaves it as it
    was, a transcript smaller than the stored offset is rescanned from the start, and only a newer
    entry replaces it."""
    state_path = os.path.join(
        tempfile.gettempdir(), f"statusline-advisor-transcript-{session_id}.json"
    )
    try:
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    entry = state.get("entry")
    if not (isinstance(entry, dict) and isinstance(entry.get("available"), bool)):
        entry = None
    if not transcript_path:
        return entry

    offset = state.get("offset") if state.get("path") == transcript_path else 0
    try:
        if not isinstance(offset, int) or not 0 <= offset <= os.path.getsize(
            transcript_path
        ):
            offset = 0
        found, resume = _scan_advisor_entries(transcript_path, offset)
    except OSError:
        return entry
    entry = found or entry
    new_state = {"path": transcript_path, "offset": resume, "entry": entry}
    if new_state != state:
        try:
            write_json_atomic(state_path, new_state)
        except OSError:
            pass
    return entry


def _get_advisor_model(session_id: str, transcript_path: str | None = None):
    """The advisor to show — an alias like "opus" or a full model id — or None.

    The harness writes an `advisor_tool` attachment into the transcript at every advisor add or
    removal, and the last one gives the current state: `available: true` carries the model,
    `available: false` means no advisor, whatever the command line says. Without any such entry,
    the `--advisor` flag of the `claude` process is the value (`process_flags`, read once per
    session). The per-message `advisorModel` field is not used: it outlives a removal.
    `CLAUDE_CODE_DISABLE_ADVISOR_TOOL` set to a truthy value shows nothing. The raw value is
    returned: `_short_model` does the capitalising and, for a full id, the version."""
    if os.environ.get(_ADVISOR_DISABLED_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return None
    entry = _transcript_advisor(session_id, transcript_path)
    if entry is not None:
        return entry.get("model") if entry["available"] else None
    return process_flags(session_id).advisor


@dataclass
class ProxyStats:
    """Provider-agnostic contract a compression-proxy adapter (`_xx_stats`) must return, consumed
    by `_fmt_compression_proxy_line` — adding a provider means writing one such function returning
    this shape, nothing else in this module changes.

    `net_tokens` carries the sign (positive = the proxy is paying for itself); `percent` is always
    positive, read alongside the sign. `bust_count` is `None` for a provider that doesn't track
    cache busts at all — distinct from `0`, a provider that tracks them and found none this
    session."""

    provider_name: str
    net_tokens: int
    percent: float
    bust_count: int | None = None


def _headroom_raw_stats() -> dict | None:
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
        pass

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
        pass
    return payload


def _headroom_stats() -> ProxyStats | None:
    """Headroom's `_xx_stats` adapter: net effect of its compression, as a `ProxyStats`.

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
    flattering the tool.
    """
    stats = _headroom_raw_stats()
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
    return ProxyStats(
        provider_name="Headroom",
        net_tokens=net,
        percent=abs(net) / before * 100,
        bust_count=busts if isinstance(busts, int) else 0,
    )


def _fmt_compression_proxy_line(stats: ProxyStats) -> str:
    """One row for any compression-proxy provider: one verdict, one magnitude, coloured by the
    sign of `stats.net_tokens`. Provider-agnostic — consumes only the `ProxyStats` contract, so a
    new `_xx_stats` adapter needs no change here.

    Deliberately not a neutral indicator — a reader who has never heard of prompt caching must be
    able to tell in one glance whether the tool is helping, so the wording flips with the sign
    instead of staying constant and leaving the reader to work out what a minus means.
    """
    amount = abs(stats.net_tokens)
    amount_txt = f"{amount // 1000}k" if amount >= 1000 else str(amount)
    pct_txt = f"{stats.percent:.1f}%" if stats.percent < 10 else f"{stats.percent:.0f}%"
    if stats.net_tokens > 0:
        colour, face, verb, name = GREEN, "🤑", "saved", "savings"
    else:
        colour, face, verb, name = RED, "💸", "lost", "losses"
    bust_txt = (
        f"{SEP}{stats.bust_count} cache bust{'s' if stats.bust_count != 1 else ''}"
        if stats.bust_count
        else ""
    )
    return (
        f"{stats.provider_name} {face}{SEP}{colour}{pct_txt} {name}"
        f"{SEP}{colour}{amount_txt} tokens {verb}{RESET}{bust_txt}"
    )


_PROXY_PROVIDERS: tuple[Callable[[], ProxyStats | None], ...] = (_headroom_stats,)


def _compression_proxy_stats() -> str | None:
    """The active compression proxy's rendered row, or None when none is running."""
    for provider_stats in _PROXY_PROVIDERS:
        stats = provider_stats()
        if stats is not None:
            return _fmt_compression_proxy_line(stats)
    return None


def _rate_limit_state_path(session_id: str) -> str:
    """Must stay byte-identical to `pc-kanban/skills/kanban/scripts/next_card_recommendation.py::_rate_limit_state_path`.
    Deliberately duplicated (decoupled plugins), not shared."""
    return os.path.join(
        tempfile.gettempdir(),
        f"pc-statusline-ratelimits-{os.getuid()}-{session_id}.json",
    )


def _write_rate_limit_state(data: dict, session_id) -> None:
    """Write the session's state file: server-side aggregated rate limits
    (`five_hour`/`seven_day`) and the context-window `used_percentage`
    (key `context_used_percentage`); pc-kanban reads it to decide session-vs-spawn. One
    write per render, even with no rate limit; errors are swallowed — a statusline render
    must never fail."""
    if not session_id:
        return
    state = {}
    for key in ("five_hour", "seven_day"):
        used = _safe_get(data, f"rate_limits.{key}.used_percentage")
        if used is None:
            continue
        state[key] = {
            "used_percentage": used,
            "resets_at": _safe_get(data, f"rate_limits.{key}.resets_at"),
        }
    context_used = _safe_get(data, "context_window.used_percentage")
    if context_used is not None:
        state["context_used_percentage"] = context_used
    if not state:
        return
    try:
        with open(_rate_limit_state_path(session_id), "w", encoding="utf-8") as handle:
            json.dump(state, handle)
    except OSError:
        pass


_GIT_TIMEOUT_S = 0.3


def _repo_branch(start_dir):
    """`"<basename(repo-root)>{SEP}🌿 <branch>"` for the git repo `start_dir` sits in (or a subdir of),
    or `None` outside a git repo / on any lookup failure: timeout, no git binary, detached-HEAD
    edge cases. `CLAUDE_PROJECT_DIR` is often empty in Claude Code and `${CLAUDE_PROJECT_DIR}`-style
    prompts don't render, so this is the one place a session can see at a glance which repo/branch
    it is actually sitting in. Untruncated — `build_lines` truncates the branch itself when the
    short layout needs the room.

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
        return f"{os.path.basename(repo_root)}{SEP}🌿 {branch_name}"
    except Exception:
        return None


_FORGE_REMOTE_RE = re.compile(r"^(?:git@|https://|http://)([^:/]+)[:/](.+?)(?:\.git)?$")


def _forge_repo_slug(remote_url: str):
    """`(hostname, "org/repo")` from a git remote URL, SSH or HTTPS form — or `None` if the URL
    doesn't match either shape."""
    m = _FORGE_REMOTE_RE.match(remote_url.strip())
    return (m.group(1), m.group(2)) if m else None


def _payload_repo_slug(repo: object) -> tuple[str, str] | None:
    """`(hostname, "owner/name")` from the payload's `workspace.repo` block, or `None` when the
    block is absent, not a dict, or missing one of `host`/`owner`/`name` — the caller then asks
    git."""
    if not isinstance(repo, dict):
        return None
    host, owner, name = repo.get("host"), repo.get("owner"), repo.get("name")
    if not (host and owner and name):
        return None
    return (host, f"{owner}/{name}")


def _repo_url(start_dir: str, repo: object = None) -> str | None:
    """URL of the current branch's page on GitHub/GitLab for the repo `start_dir` sits in, or the
    repo's root page when the branch has no upstream yet (a fresh local branch has no remote tree
    to link to — the branch-specific URL would 404), or `None` outside a repo / no `origin` remote
    / an unrecognised forge / any lookup failure. Matched by "github"/"gitlab" appearing in the
    remote's hostname, which covers both the public services and the common self-hosted case.

    `repo` is the payload's `workspace.repo` block (`host`/`owner`/`name`) when Claude Code
    provides one; it saves the `git remote get-url origin` call. Absent or incomplete, the slug
    comes from git."""
    if not start_dir:
        return None
    try:
        slug = _payload_repo_slug(repo)
        if not slug:
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
            return base
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

    Three logical rows — budget + cost, repo + session, model — drawn on five lines (cost, budget,
    session, repo, model), preceded by a rate-limit alert row while a window is about to run out
    and followed by the compression-proxy row while one is up. The logical rows use the long layout
    when every one of them fits the terminal width (`_term`) with its two groups side by side, the
    short layout otherwise; the alert row picks its own form the same way, so an alert showing up
    never flips the format of the rest.

    Pure + total in intent: `main()` wraps this call in a try/except so a malformed payload
    degrades to a bare fallback line instead of blanking the whole bar — but this function itself
    stays the single, testable entry point (tests.md § generator oracle)."""
    transcript_path = _safe_get(data, "transcript_path")
    session_id = _safe_get(data, "session_id", "?")
    cols = _safe_get(data, "_term")
    if not isinstance(cols, int) or cols <= 0:
        cols = _DEFAULT_COLS
    right_edge = cols - _RIGHT_MARGIN

    # ---------- Model facts ----------
    model = _safe_get(data, "model.display_name", "?")
    effort = _safe_get(data, "effort.level")
    thinking_on = _safe_get(data, "thinking.enabled", False)
    fast_on = _safe_get(data, "fast_mode", False)
    style = str(_safe_get(data, "output_style.name", "default")).split(":")[-1]

    effort_txt = f"💪 {effort}" if effort else f"{DIM}💪 —{RESET}"
    thinking_txt = "🧠 ON" if thinking_on else f"{DIM}🧠 off{RESET}"
    fast_txt = "⚡️ fast" if fast_on else f"{DIM}🐢 normal{RESET}"

    advisor_model = _short_model(_get_advisor_model(session_id, transcript_path))

    model_txt = f"{CYAN}{BOLD}🤖 {model}{RESET}"
    advisor_txt = f"{YELLOW}🧐 {advisor_model}{RESET}" if advisor_model else None
    # Long form: everything. Short form: only what is worth the room — the model, its effort, the
    # fast mode when it is on, the advisor when there is one.
    model_long = [model_txt, effort_txt, fast_txt]
    model_short = [model_txt, effort_txt]
    if fast_on:
        model_short.append(fast_txt)
    if advisor_txt:
        model_long.append(advisor_txt)
        model_short.append(advisor_txt)
    model_long += [thinking_txt, f"🎨 {style}{RESET}"]

    # ---------- Session facts ----------
    short_session_id = (
        session_id[:8] if session_id and session_id != "?" else session_id
    )
    session_txt = (
        _hyperlink(short_session_id, f"file://{transcript_path}")
        if transcript_path
        else short_session_id
    )

    duration_ms = _safe_get(data, "cost.total_duration_ms", 0) or 0
    start_dt = datetime.now() - timedelta(milliseconds=duration_ms)
    subagent_count = _count_subagents(transcript_path, session_id)
    subagent_txt = None
    if subagent_count:
        subagent_txt = f"{CYAN}🧬 {subagent_count} subagent{'s' if subagent_count > 1 else ''}{RESET}"
        if transcript_path:
            session_dir = os.path.join(os.path.dirname(transcript_path), session_id)
            subagent_txt = _hyperlink(subagent_txt, f"file://{session_dir}/")

    # ---------- Repo facts ----------
    repo_dir = _safe_get(data, "workspace.project_dir") or _safe_get(data, "cwd")
    repo_branch = _repo_branch(repo_dir)
    repo_url = (
        _repo_url(repo_dir, _safe_get(data, "workspace.repo")) if repo_branch else None
    )

    # ---------- Budget facts ----------
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
        warmed_up = cache_c > 0 or cache_r > 0
        color = _color_for_hit_rate(hit_rate, warmed_up)
        cache_txt = f"{color}{hit_rate:.0f}%{RESET}"
    else:
        cache_txt = f"{DIM}—{RESET}"

    def permanent_rows(short: bool):
        """The logical rows as `(left_parts, right_parts)` pairs, in the long or the short form:
        budget + cost, repo + session, and the model row, whose parts all sit in the right group
        (drawn last, right-aligned; reduced to the essentials in the short form). A pair is fitted
        as one line and drawn on two (`_render_pair`)."""
        sep = _SEP_SHORT if short else SEP
        min_gap = _MIN_GAP_SHORT if short else _MIN_GAP

        context_txt = f"{bar} {_color_for_pct(pct)}{pct}%{RESET}"
        if not short:
            context_txt = f"Context window {context_txt}"
        budget_left = [context_txt, f"Cache hit {cache_txt}{RESET}"]
        if subagent_txt:
            budget_left.append(subagent_txt)
        cost_right = [f"💲 {cost:.2f} (API-equiv)"]

        session_label = "" if short else "Session "
        started_label = "" if short else "Started at "
        started_txt = start_dt.strftime("%H:%M" if short else "%d/%m %H:%M")
        session_right = [
            f"{MAGENTA}{session_label}🆔 {session_txt}{RESET}",
            f"🕐 {started_label}{started_txt}{RESET}",
        ]

        repo_left: list[str] = []
        if repo_branch:
            repo_txt = f"📁 {repo_branch}"
            if short:
                # The branch name is the only elastic part of the row: it gets whatever width
                # the repo name and the session group leave free.
                repo_name, _, branch_part = repo_branch.partition(SEP)
                branch_name = branch_part.split(" ", 1)[-1]
                fixed = _visible_len(f"📁 {repo_name}{sep}🌿 ")
                room = (
                    right_edge - min_gap - _visible_len(sep.join(session_right)) - fixed
                )
                branch_name = _truncate_end(branch_name, max(room, _MIN_BRANCH_COLS))
                repo_txt = f"📁 {repo_name}{sep}🌿 {branch_name}"
            repo_left.append(_hyperlink(repo_txt, repo_url) if repo_url else repo_txt)

        return [
            (budget_left, cost_right),
            (repo_left, session_right),
            ([], model_short if short else model_long),
        ]

    rows = permanent_rows(False)
    sep, min_gap = SEP, _MIN_GAP
    if not all(
        _row_width(left, right, sep, min_gap) <= right_edge for left, right in rows
    ):
        rows = permanent_rows(True)
        sep, min_gap = _SEP_SHORT, _MIN_GAP_SHORT

    def alert_row(short: bool) -> tuple[list[str], list[str]] | None:
        """The rate-limit alert as a `(left_parts, right_parts)` pair — 5h on the left, 7d on the
        right when both warn, a lone window on the left — or `None` while neither does."""
        five = _rate_limit_alert("five_hour", data, short)
        seven = _rate_limit_alert("seven_day", data, short)
        if five and seven:
            return ([five], [seven])
        only = five or seven
        return ([only], []) if only else None

    lines: list[str] = []
    alert = alert_row(False)
    if alert:
        alert_sep, alert_gap = SEP, _MIN_GAP
        if _row_width(*alert, alert_sep, alert_gap) > right_edge:
            alert = cast("tuple[list[str], list[str]]", alert_row(True))
            alert_sep, alert_gap = _SEP_SHORT, _MIN_GAP_SHORT
        lines.append(_render_flex(*alert, right_edge, alert_sep, alert_gap))

    for left, right in rows:
        lines.extend(_render_pair(left, right, right_edge, sep))

    # ---------- Optional row : the active compression proxy, only while one is up ----------
    proxy_line = _compression_proxy_stats()
    if proxy_line:
        lines.append(proxy_line)

    return lines


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        print("[status line: invalid JSON]")
        return
    # Dumped as received, before the injections below add to the payload.
    dump_debug_payload(data, _safe_get(data, "session_id"), "statusline")
    # The harness's own `rate_limits` always wins; only when it is absent (any non-Anthropic
    # provider) is the active provider's quota injected — before the state write, so pc-kanban
    # reads the quota of the provider this session is actually spending.
    if isinstance(data, dict) and not data.get("rate_limits"):
        injected = provider_rate_limits()
        if injected:
            data["rate_limits"] = injected
    if isinstance(data, dict):
        data["_term"] = _term_cols()
    _write_rate_limit_state(data, _safe_get(data, "session_id"))
    try:
        print("\n".join(build_lines(data)))
    except Exception:
        print("⬡")


if __name__ == "__main__":
    main()
