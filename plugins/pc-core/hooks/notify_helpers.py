#!/usr/bin/env python3
"""notify_helpers — cross-terminal desktop notification for hook JSON output.

Call sites (hooks handlers)
call notify() and merge the result into their hook output — none of them
need to know which terminal or multiplexer is running, that knowledge
lives here.

Why a helper at all: `terminalSequence` carries one OSC variant, but
different terminals need different OSC codes, and cmux/wmux actively
suppress raw OSC from a process they recognize as Claude Code — they need
their own `<mux> notify` CLI instead. See _detect_terminal() and notify()
for the actual dispatch logic.
"""

import os
import subprocess


def _detect_terminal() -> str:
    """Best-effort terminal/multiplexer identification from environment
    variables. cmux/wmux checked first, unconditionally: cmux runs on top
    of Ghostty, so TERM_PROGRAM can say "ghostty" while CMUX_SURFACE_ID is
    *also* set — checking Ghostty first would misdetect it. "unknown"
    falls back to the OSC 777 default in _osc_sequence(), never to
    nothing."""
    if os.environ.get("CMUX_SURFACE_ID"):
        return "cmux"
    if os.environ.get("WMUX_SURFACE_ID"):
        return "wmux"

    term_program = os.environ.get("TERM_PROGRAM", "").lower()

    if "iterm" in term_program or os.environ.get("ITERM_SESSION_ID"):
        return "iterm2"
    if "wezterm" in term_program or os.environ.get("WEZTERM_PANE"):
        return "wezterm"
    if os.environ.get("WT_SESSION"):
        return "windows_terminal"
    if "warp" in term_program:
        return "warp"
    if os.environ.get("KITTY_WINDOW_ID"):
        return "kitty"
    if "ghostty" in term_program or os.environ.get("GHOSTTY_RESOURCES_DIR"):
        return "ghostty"
    if os.environ.get("TERM", "").startswith("rxvt"):
        return "urxvt"
    return "unknown"


def _osc_sequence(terminal: str, title: str, message: str) -> str:
    """OSC notification sequence for a detected terminal. Unrecognized
    terminals get the OSC 777 default — detection only adds coverage,
    never removes it."""
    if terminal in ("iterm2", "wezterm", "windows_terminal"):
        # OSC 9: ESC ] 9 ; message BEL — no title slot, fold it in ourselves.
        return f"\033]9;{title}: {message}\007"

    if terminal == "kitty":
        # OSC 99: two escapes, d=0 (more coming) then d=1 (done), ST (ESC \)
        # not BEL. Built from kitty's own protocol docs, unverified against
        # a live Kitty session.
        return f"\033]99;i=1:d=0;{title}\033\\\033]99;i=1:d=1;{message}\033\\"

    # ghostty, warp, urxvt, unknown: OSC 777 — Claude Code's documented default.
    return f"\033]777;notify;{title};{message}\007"


def notify(title: str, message: str) -> dict:
    """Return the hook-output fields for a cross-terminal notification.

    Side effect: dispatches `<mux> notify` directly when running inside
    cmux or wmux. Merge the returned dict into your hook's own output —
    it only ever sets `systemMessage` and, outside a mux,
    `terminalSequence`, never `hookSpecificOutput` or anything else.
    """
    output: dict = {"systemMessage": message}

    terminal = _detect_terminal()
    if terminal in ("cmux", "wmux"):
        try:
            subprocess.run(
                [terminal, "notify", "--title", title, "--body", message],
                check=False,
                timeout=5,
                # our stdout IS the hook's JSON channel — anything the CLI
                # itself prints would land ahead of it and break the
                # "starts with {" parse.
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass  # CLI unreachable — systemMessage above still gets through
    else:
        output["terminalSequence"] = _osc_sequence(terminal, title, message)

    return output
