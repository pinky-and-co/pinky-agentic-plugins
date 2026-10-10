"""Shared layer for the two status-line entry points (`statusline.py`, `subagent_statusline.py`).

Only what is genuinely identical between the two lives here: the ANSI palette, the Windows/UTF-8
stdout setup, the bar-rendering core, the reading of the `claude` process's launch flags
(`--advisor`, `--debug`) and the debug payload dump they both feed. Everything the two scripts do
*differently* on purpose —
`_visible_len` (wide-char aware vs naive), `_color_for_pct` thresholds, `_short_model`'s empty-value
placeholder — stays inline in each script, pinned by that script's own tests.

Not an entry point: no `main()`, imported by its two consumers the way `hygiene/vendor/policy_lib.py`
is imported by its own.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# --- ANSI palette — the subset both scripts share ---------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
GREY = "\033[90m"


def enable_ansi_on_windows() -> None:
    """Turn on ANSI escape processing for classic cmd.exe/PowerShell — a no-op on Mac/Linux and
    on Windows Terminal (which already has it). Never raises: a failure just means no colours."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # Windows-only, guarded by os.name check above
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            enable_virtual_terminal_processing = 0x0004
            kernel32.SetConsoleMode(
                handle, mode.value | enable_virtual_terminal_processing
            )
    except Exception:
        # Old cmd.exe, missing permissions… — continue without colours rather than crash.
        pass


def force_utf8_stdout() -> None:
    """Force UTF-8 on stdout (Windows consoles sometimes default to cp1252, which breaks emoji).
    `reconfigure()` has existed since Python 3.7; guarded because a redirected stream may lack it."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # absent only on a non-TextIOWrapper stream, caught below
    except Exception:
        pass


def render_bar(
    pct: float, *, width: int, color: str, filled: str = "█", empty: str = "░"
) -> str:
    """A progress bar: `pct` clamped to 0-100, `width` cells, the filled run wrapped in `color`
    and the remainder dimmed. The caller resolves `color` itself — the two scripts colour the
    bar on different thresholds, and that difference stays theirs."""
    pct = max(0, min(100, pct))
    n = round(pct * width / 100)
    return f"{color}{filled * n}{RESET}{DIM}{empty * (width - n)}{RESET}"


# --- the `claude` process's launch flags — read once per session, cached ------------------------

_ADVISOR_FLAG_RE = re.compile(r"--advisor(?:=|\s+)(\S+)")
# A whole `--debug` token, with or without `=<filter>`: `--debug-file` is a different flag.
_DEBUG_FLAG_RE = re.compile(r"(?:^|\s)--debug(?:=\S*)?(?=\s|$)")
# A session id is used in a file name: nothing that could climb out of its directory.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][\w.-]*")
_DEBUG_KEEP = 5


def _ps(*args: str) -> str:
    """Standard output of `ps <args>`; raises on a missing binary, a timeout or a non-zero exit."""
    return subprocess.run(
        ["ps", *args], capture_output=True, text=True, timeout=2, check=True
    ).stdout


def _is_claude(args: str) -> bool:
    """Whether the first token of a command line is the `claude` binary (not a process citing it)."""
    first = args.split(None, 1)[0] if args.strip() else ""
    return os.path.basename(first) == "claude"


def _claude_args_by_session(session_id: str) -> str | None:
    """Command line of the `claude` process launched with `--session-id <session_id>`, or None."""
    for line in _ps("-axo", "args=").splitlines():
        if _is_claude(line) and (
            f"--session-id {session_id}" in line or f"--session-id={session_id}" in line
        ):
            return line
    return None


def _claude_args_by_ancestry() -> str | None:
    """Command line of the nearest `claude` ancestor of this process, or None.

    Fallback for a session whose id is not on the command line (after `/clear`, outside cmux)."""
    pid = os.getppid()
    for _ in range(8):
        out = _ps("-o", "ppid=,args=", "-p", str(pid)).strip()
        if not out:
            return None
        ppid, _, args = out.partition(" ")
        if _is_claude(args.strip()):
            return args
        pid = int(ppid)
        if pid <= 1:
            return None
    return None


def write_json_atomic(path: str, payload: object) -> None:
    """Write `payload` as JSON to `path` through a sibling temp file (mode 0o600) and
    `os.replace`, so a concurrent reader sees the old file or the new one, never half of it.

    The temp file is removed on any failure before the replace, a serialisation error included;
    the exception then propagates."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class ProcessFlags:
    """The launch flags of a `claude` process the status line acts on: the `--advisor` value
    (None when absent) and whether `--debug` is there."""

    advisor: str | None
    debug: bool


def process_flags(session_id: str) -> ProcessFlags:
    """The `ProcessFlags` of the session's `claude` process.

    The command line never changes during the process's life, so one `ps` reading per session is
    cached in `statusline-advisor-<session_id>.json` — "no flag" included, a known result and not
    an absence of reading. A failed `ps` caches nothing (the next call retries) and answers
    `ProcessFlags(None, False)`. The file also reads the shape that carried `--advisor` alone: no `debug`
    key means no `--debug`. Only the two flag values leave this function, never the command
    line."""
    cache = os.path.join(tempfile.gettempdir(), f"statusline-advisor-{session_id}.json")
    try:
        with open(cache, encoding="utf-8") as handle:
            cached = json.load(handle)
        return ProcessFlags(cached["advisor"], bool(cached.get("debug", False)))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass

    try:
        args = _claude_args_by_session(session_id) or _claude_args_by_ancestry() or ""
    except (subprocess.SubprocessError, OSError, ValueError):
        return ProcessFlags(None, False)

    match = _ADVISOR_FLAG_RE.search(args)
    advisor = match.group(1) if match else None
    debug = bool(_DEBUG_FLAG_RE.search(args))
    try:
        write_json_atomic(cache, {"advisor": advisor, "debug": debug})
    except OSError:
        pass
    return ProcessFlags(advisor, debug)


# --- debug dump — the last payloads a status-line script received ------------------------------


def dump_debug_payload(payload: object, session_id: object, name: str) -> None:
    """Keep `payload` in `~/.claude/debug/<session_id>__pc-statusline__<name>.json` while the
    session's `claude` process runs with `--debug`; write nothing otherwise.

    The file is a JSON array of the last `_DEBUG_KEEP` distinct payloads, oldest first, and is
    rewritten only when `payload` differs from the last one stored. Every error is swallowed: a
    status line renders whatever happens to its debug file."""
    try:
        if not (isinstance(session_id, str) and _SESSION_ID_RE.fullmatch(session_id)):
            return
        if not process_flags(session_id).debug:
            return
        path = (
            Path.home()
            / ".claude"
            / "debug"
            / f"{session_id}__pc-statusline__{name}.json"
        )
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = []
        if not isinstance(stored, list):
            stored = []
        if stored and stored[-1] == payload:
            return
        kept = [item for item in stored if item != payload][-(_DEBUG_KEEP - 1) :]
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(str(path), [*kept, payload])
    except Exception:
        pass
