"""Shared status-line layer (`_common.py`) — the surface both entry points import.

Same oracle strategy as its two consumers (tests.md § generator oracle): `render_bar` builds
output, so it gets a whole-string equality oracle, not a fragment check.
"""

import sys
from pathlib import Path

HOOKS = Path(__file__).parent.parent / "plugins" / "pc-statusline" / "scripts"
sys.path.insert(0, str(HOOKS))

import _common as c  # noqa: E402


def test_render_bar_oracle():
    # 40% of a 10-cell bar = round(4.0) = 4 filled + 6 empty, filled run wrapped in the passed
    # colour, remainder dimmed — the exact string, separators and resets included.
    assert c.render_bar(40, width=10, color=c.GREEN) == (
        f"{c.GREEN}{'█' * 4}{c.RESET}{c.DIM}{'░' * 6}{c.RESET}"
    )


def test_render_bar_clamps_out_of_range_pct():
    assert c.render_bar(150, width=5, color=c.RED) == c.render_bar(
        100, width=5, color=c.RED
    )
    assert c.render_bar(-20, width=5, color=c.RED) == c.render_bar(
        0, width=5, color=c.RED
    )


def test_render_bar_honours_custom_fill_chars():
    assert c.render_bar(50, width=4, color=c.GREEN, filled="#", empty="-") == (
        f"{c.GREEN}##{c.RESET}{c.DIM}--{c.RESET}"
    )


def test_setup_helpers_never_raise_off_windows():
    # On Mac/Linux both are near no-ops; the contract is only that they never blow up.
    c.enable_ansi_on_windows()
    c.force_utf8_stdout()
