"""Shared layer for the two status-line entry points (`statusline.py`, `subagent_statusline.py`).

Only what is genuinely identical between the two lives here: the ANSI palette, the Windows/UTF-8
stdout setup, and the bar-rendering core. Everything the two scripts do *differently* on purpose —
`_visible_len` (wide-char aware vs naive), `_color_for_pct` thresholds, `_short_model`'s empty-value
placeholder — stays inline in each script, pinned by that script's own tests.

Not an entry point: no `main()`, imported by its two consumers the way `hygiene/vendor/policy_lib.py`
is imported by its own.
"""

from __future__ import annotations

import os
import sys

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

        kernel32 = ctypes.windll.kernel32
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
        sys.stdout.reconfigure(encoding="utf-8")
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
