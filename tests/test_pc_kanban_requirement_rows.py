"""Tests for plugins/pc-kanban/hooks/requirement-rows/pc_kanban_requirement_rows.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-kanban"
        / "hooks"
        / "requirement-rows"
    ),
)

from pc_kanban_requirement_rows import RequirementRow, holders, requirement_rows  # noqa: E402


def _card(*rows: str) -> str:
    return "## Requirements\n\n| Id | P | E | C |\n|---|---|---|---|\n" + "".join(
        f"{r}\n" for r in rows
    )


def test_requirement_rows_oracle() -> None:
    long = "| PKG-0025-3 | MUST | s | c | extra | more |"
    cards = [
        (
            "a",
            _card(
                "| PKG-0025-1 | MUST | s | c | |",
                "| PKG-0025-2 | MUST |",
                long,
            ),
        )
    ]
    assert requirement_rows(cards) == [
        RequirementRow("PKG-0025-1", "MUST", "s", "c", "a", "-"),
        RequirementRow("PKG-0025-2", "MUST", "", "", "a", "-"),
        RequirementRow("PKG-0025-3", "MUST", "s", "c | extra | more", "a", "-"),
    ]
    assert [r.check for r in requirement_rows(cards)] == ["c", "", "c | extra | more"]
    assert requirement_rows(cards)[2].row == long


def test_rows_differing_only_by_slug_or_row_are_equal() -> None:
    rows = requirement_rows(
        [
            ("a", _card("| PKG-0025-1 | MUST | s | c |")),
            ("b", _card("|PKG-0025-1|  MUST | s | c  |")),
        ]
    )
    assert rows[0] != rows[1].__class__("x", "", "", "", "a", "")
    assert rows[0] == rows[1]
    assert (rows[0].slug, rows[1].slug) == ("a", "b")
    assert len({*rows}) == 1


def _board(tmp_path: Path, head: str) -> Path:
    todo = tmp_path / "1-todo"
    todo.mkdir(parents=True)
    (todo / "kid.md").write_text(f"---\n{head}---\n# kid\n", encoding="utf-8")
    return tmp_path


def test_holders_reads_serves(tmp_path: Path) -> None:
    assert holders(_board(tmp_path, "serves: demo\n"), "demo") == ["kid"]


def test_holders_no_longer_reads_the_old_sert_field(tmp_path: Path) -> None:
    assert holders(_board(tmp_path, "sert: demo\n"), "demo") == []
