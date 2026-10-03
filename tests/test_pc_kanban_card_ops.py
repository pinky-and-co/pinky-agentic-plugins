"""Tests for plugins/pc-kanban/hooks/card-ops/pc_kanban_card_ops.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-kanban"
    / "hooks"
    / "card-ops"
    / "pc_kanban_card_ops.py"
)


def _run(board: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--board", str(board), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _card(
    root: Path,
    column: str,
    name: str = "demo",
    head: str = "model: sonnet\nblocked_by: null\nrequirements: [PKG-0001-01]\n",
) -> Path:
    card = root / "CHANTIER0001-x" / column / f"{name}.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text(
        f"---\ndate: 2026-01-01\n{head}---\n# Demo\n\n## Done when\n\nx\n",
        encoding="utf-8",
    )
    story = root / "CHANTIER0001-x" / "story.md"
    if not story.exists():
        story.write_text("| PKG-0001-01 | MUST | x | y |\n", encoding="utf-8")
    return card


def test_select_moves_the_card_and_stamps_the_date(tmp_path: Path) -> None:
    card = _card(tmp_path, "1-todo")
    result = _run(tmp_path, "select", "demo")
    dest = tmp_path / "CHANTIER0001-x" / "2-selected-for-dev" / "demo.md"
    assert result.returncode == 0, result.stderr
    assert not card.exists()
    assert not dest.read_text().startswith("---\ndate: 2026-01-01\n")
    assert "model: sonnet" in dest.read_text()


def test_select_refuses_a_card_without_model(tmp_path: Path) -> None:
    card = _card(tmp_path, "1-todo", head="requirements: [PKG-0001-01]\n")
    result = _run(tmp_path, "select", "demo")
    assert result.returncode == 1
    assert "`model:`" in result.stderr
    assert card.exists()


def test_select_refuses_a_blocked_card(tmp_path: Path) -> None:
    card = _card(
        tmp_path,
        "1-todo",
        head="model: opus\nblocked_by: other\nrequirements: [PKG-0001-01]\n",
    )
    result = _run(tmp_path, "select", "demo")
    assert result.returncode == 1
    assert "blocked_by 'other' is not done" in result.stderr
    assert card.exists()


def test_unknown_slug_is_reported(tmp_path: Path) -> None:
    result = _run(tmp_path, "select", "nope")
    assert result.returncode != 0
    assert "no card 'nope'" in result.stderr


def test_a_slug_found_twice_is_refused(tmp_path: Path) -> None:
    _card(tmp_path, "1-todo")
    other = tmp_path / "CHANTIER0098-y" / "1-todo" / "demo.md"
    other.parent.mkdir(parents=True)
    other.write_text("---\nmodel: haiku\n---\n", encoding="utf-8")
    result = _run(tmp_path, "select", "demo")
    assert result.returncode != 0
    assert "several cards match" in result.stderr


def test_archive_resolves_the_slug_and_archives(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    card = _card(board, "4-done")
    result = _run(board, "archive", "demo")
    assert result.returncode == 0, result.stderr
    assert not card.exists()
    assert list((board / "_ARCHIVE" / "CHANTIER0001-done").glob("*_demo.md"))


def test_drop_needs_a_reason_and_records_it(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    card = _card(board, "0-ideas")
    assert _run(board, "drop", "demo").returncode == 2
    assert card.exists()
    result = _run(board, "drop", "demo", "superseded", "by", "bar")
    assert result.returncode == 0, result.stderr
    dropped = next((board / "_ARCHIVE" / "CHANTIER0001-dropped").glob("*_demo.md"))
    assert "superseded by bar" in dropped.read_text()


def test_select_lists_every_unmet_condition(tmp_path: Path) -> None:
    card = tmp_path / "CHANTIER0001-x" / "1-todo" / "demo.md"
    card.parent.mkdir(parents=True)
    card.write_text("---\ntitle: bare\n---\n# Demo\n", encoding="utf-8")
    result = _run(tmp_path, "select", "demo")
    assert result.returncode == 1
    for needle in ("`requirements:`", "`model:`", "`## Done when`"):
        assert needle in result.stderr
    assert card.exists()


def test_select_accepts_a_blocker_already_archived_done(tmp_path: Path) -> None:
    _card(
        tmp_path,
        "1-todo",
        head="model: opus\nblocked_by: base\nrequirements: [PKG-0001-01]\n",
    )
    archived = tmp_path / "_ARCHIVE" / "CHANTIER0001-done" / "2026-01-02_base.md"
    archived.parent.mkdir(parents=True)
    archived.write_text("x", encoding="utf-8")
    assert _run(tmp_path, "select", "demo").returncode == 0


def test_select_flags_a_dropped_blocker_for_the_obsolescence_check(
    tmp_path: Path,
) -> None:
    card = _card(
        tmp_path,
        "1-todo",
        head="model: opus\nblocked_by: base\nrequirements: [PKG-0001-01]\n",
    )
    dropped = tmp_path / "_ARCHIVE" / "CHANTIER0001-dropped" / "2026-01-02_base.md"
    dropped.parent.mkdir(parents=True)
    dropped.write_text("x", encoding="utf-8")
    result = _run(tmp_path, "select", "demo")
    assert result.returncode == 1
    assert "was dropped — run the obsolescence check" in result.stderr
    assert card.exists()


def test_select_refuses_a_requirement_the_story_does_not_declare(
    tmp_path: Path,
) -> None:
    card = _card(tmp_path, "1-todo", head="model: opus\nrequirements: [PKG-0099-99]\n")
    result = _run(tmp_path, "select", "demo")
    assert result.returncode == 1
    assert "requirement PKG-0099-99 is not declared" in result.stderr
    assert card.exists()


def test_select_inserts_a_missing_date(tmp_path: Path) -> None:
    card = tmp_path / "CHANTIER0001-x" / "1-todo" / "demo.md"
    card.parent.mkdir(parents=True)
    card.write_text(
        "---\nmodel: sonnet\nrequirements: [PKG-0001-01]\n---\n## Done when\nx\n",
        encoding="utf-8",
    )
    (tmp_path / "CHANTIER0001-x" / "story.md").write_text(
        "| PKG-0001-01 | MUST | x | y |\n", encoding="utf-8"
    )
    assert _run(tmp_path, "select", "demo").returncode == 0
    moved = tmp_path / "CHANTIER0001-x" / "2-selected-for-dev" / "demo.md"
    assert moved.read_text().startswith("---\ndate: 20")


def test_force_selects_a_card_without_requirements(tmp_path: Path) -> None:
    _card(tmp_path, "1-todo", head="model: sonnet\nrequirements: []\n")
    assert _run(tmp_path, "select", "demo").returncode == 1
    assert _run(tmp_path, "select", "demo", "--force").returncode == 0


def test_force_does_not_skip_the_other_conditions(tmp_path: Path) -> None:
    card = _card(tmp_path, "1-todo", head="requirements: []\n")
    result = _run(tmp_path, "select", "demo", "--force")
    assert result.returncode == 1
    assert "`model:`" in result.stderr
    assert "`requirements:`" not in result.stderr
    assert card.exists()
