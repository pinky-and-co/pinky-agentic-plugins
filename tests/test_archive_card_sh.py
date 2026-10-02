"""Tests for plugins/pc-kanban/skills/chantier/scripts/archive_card.sh."""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-kanban"
    / "skills"
    / "chantier"
    / "scripts"
    / "archive_card.sh"
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def _card(root: Path, column: str, reviewed: bool = False) -> Path:
    chantier = root / "_KANBAN" / "CHANTIER0099-demo"
    card = chantier / column / "card.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    body = "---\ntitle: demo\nrequirements: []\n---\n# Card\n"
    if reviewed:
        body += "\n- [x] reviewed (n/a — mechanical)\n"
    card.write_text(body, encoding="utf-8")
    return card


def test_archives_from_4_done(tmp_path: Path) -> None:
    card = _card(tmp_path, "4-done")
    result = _run(str(card))
    assert result.returncode == 0, result.stderr
    archive = tmp_path / "_KANBAN" / "_ARCHIVE" / "CHANTIER0099-done"
    assert list(archive.glob("*_card.md")), "card not archived"


def test_archives_directly_from_3_in_progress_when_reviewed(tmp_path: Path) -> None:
    card = _card(tmp_path, "3-in-progress", reviewed=True)
    result = _run(str(card))
    assert result.returncode == 0, result.stderr
    archive = tmp_path / "_KANBAN" / "_ARCHIVE" / "CHANTIER0099-done"
    assert list(archive.glob("*_card.md")), "card not archived"


def test_refuses_3_in_progress_without_reviewed_box(tmp_path: Path) -> None:
    card = _card(tmp_path, "3-in-progress")
    result = _run(str(card))
    assert result.returncode == 1
    assert card.is_file(), "card must stay in place on refusal"
    assert "reviewed" in result.stderr


def test_refuses_open_columns(tmp_path: Path) -> None:
    card = _card(tmp_path, "1-todo")
    result = _run(str(card))
    assert result.returncode == 1
    assert card.is_file()


def test_refuses_missing_card(tmp_path: Path) -> None:
    result = _run(str(tmp_path / "nowhere.md"))
    assert result.returncode == 1
    assert "no such card" in result.stderr
