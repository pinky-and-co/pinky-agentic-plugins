"""Tests for plugins/pc-kanban/skills/chantier/scripts/split_goal.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-kanban"
    / "skills"
    / "chantier"
    / "scripts"
    / "split_goal.py"
)


def _run(stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _blocks(stdout: str) -> list[str]:
    """The fenced bodies of a rendered output, in order."""
    lines = stdout.split("\n")
    return [lines[i + 2] for i, line in enumerate(lines) if line.startswith("📋")]


def test_short_condition_is_one_block() -> None:
    result = _run("done when tests pass\n")
    assert result.returncode == 0
    assert result.stdout == (
        "📋 Paste 1/1 (20 characters)\n```\ndone when tests pass\n```\n"
    )


def test_cuts_at_semicolon_and_blocks_concatenate_to_the_condition() -> None:
    clause = "x" * 500
    condition = f"{clause} ; {clause} ; {clause}"
    result = _run(condition)
    assert result.returncode == 0
    blocks = _blocks(result.stdout)
    assert blocks == [f"{clause} ; ", f"{clause} ; ", clause]
    assert "".join(blocks) == condition


def test_oversized_clause_is_cut_at_a_space() -> None:
    condition = " ".join(["mot"] * 390)
    result = _run(condition)
    blocks = _blocks(result.stdout)
    assert result.returncode == 0
    assert len(blocks) == 2
    assert all(len(b) <= 790 for b in blocks)
    assert blocks[0].endswith(" ")
    assert "".join(blocks) == condition


def test_newlines_inside_the_condition_become_spaces() -> None:
    result = _run("a ;\nb\n")
    assert result.stdout == "📋 Paste 1/1 (5 characters)\n```\na ; b\n```\n"


def test_more_than_five_blocks_is_refused() -> None:
    result = _run(" ".join(["mot"] * 1100))
    assert result.returncode == 1
    assert result.stdout == ""
    assert "5 blocks" in result.stderr


def test_empty_input_is_a_usage_error() -> None:
    result = _run("  \n")
    assert result.returncode == 2
