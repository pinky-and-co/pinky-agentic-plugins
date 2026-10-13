"""Tests for plugins/pc-kanban/skills/chantier/scripts/story_view.py."""

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
    / "story_view.py"
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _card(
    path: Path,
    rows: list[str] | None = None,
    cites: str = "",
    blocked_by: str = "null",
    questions: list[str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"---\nblocked_by: {blocked_by}\nrequirements: [{cites}]\n---\n# {path.stem}\n"
    )
    if rows:
        body += "\n## Requirements\n\n| Id | P | E | C |\n|---|---|---|---|\n"
        body += "".join(f"| {r} | MUST | x | y |\n" for r in rows)
    if questions:
        body += "\n## Open questions\n\n" + "".join(f"- {q}\n" for q in questions)
    path.write_text(body, encoding="utf-8")


def _board(tmp_path: Path) -> Path:
    chantier = tmp_path / "_KANBAN" / "CHANTIER0025-demo"
    _card(chantier / "1-todo" / "alpha.md", ["PKG-0025-1"], questions=["why alpha?"])
    _card(chantier / "3-in-progress" / "beta.md", ["PKG-0025-2"], cites="PKG-0025-1")
    _card(
        tmp_path / "_KANBAN" / "_ARCHIVE" / "CHANTIER0025-done" / "2026-01-01_old.md",
        ["PKG-0025-12", "PKG-0025-3"],
        questions=["old question"],
    )
    return chantier


def test_shows_requirements_and_questions_of_open_cards_only(tmp_path: Path) -> None:
    out = _run(str(_board(tmp_path))).stdout
    assert "PKG-0025-1 |" in out and "<- alpha" in out
    assert "PKG-0025-2 |" in out and "<- beta" in out
    assert "PKG-0025-3" not in out
    assert "[alpha] why alpha?" in out
    assert "old question" not in out


def test_orders_a_blocked_card_after_its_blocker(tmp_path: Path) -> None:
    chantier = tmp_path / "_KANBAN" / "CHANTIER0025-demo"
    _card(chantier / "1-todo" / "a.md", blocked_by="b")
    _card(chantier / "1-todo" / "b.md")
    out = _run(str(chantier)).stdout
    assert out.index("- b\n") < out.index("- a\n")


def test_next_id_counts_archived_cards(tmp_path: Path) -> None:
    assert (
        _run("--next-id", "PKG", str(_board(tmp_path))).stdout.strip() == "PKG-0025-13"
    )


def test_next_id_counts_story_and_starts_at_one(tmp_path: Path) -> None:
    chantier = tmp_path / "_KANBAN" / "CHANTIER0025-demo"
    chantier.mkdir(parents=True)
    assert _run("--next-id", "PKG", str(chantier)).stdout.strip() == "PKG-0025-1"
    (chantier / "story.md").write_text("| PKG-0025-40 | x |\n", encoding="utf-8")
    assert _run("--next-id", "PKG", str(chantier)).stdout.strip() == "PKG-0025-41"


def test_flags_an_id_defined_only_in_an_archived_card(tmp_path: Path) -> None:
    chantier = _board(tmp_path)
    _card(chantier / "1-todo" / "gamma.md", cites="PKG-0025-3")
    out = _run(str(chantier)).stdout
    assert "Warning" in out
    assert "PKG-0025-3 cited by gamma" in out
    assert "PKG-0025-1 cited by" not in out


def test_no_warning_when_every_cited_id_is_defined(tmp_path: Path) -> None:
    assert "Warning" not in _run(str(_board(tmp_path))).stdout


def test_missing_directory_is_a_usage_error(tmp_path: Path) -> None:
    assert _run(str(tmp_path / "nope")).returncode == 2


def test_render_oracle(tmp_path: Path) -> None:
    chantier = tmp_path / "_KANBAN" / "CHANTIER0025-demo"
    _card(chantier / "1-todo" / "a.md", ["PKG-0025-1"], questions=["q?"])
    _card(chantier / "1-todo" / "b.md", cites="PKG-0025-1", blocked_by="a")
    assert _run(str(chantier)).stdout == (
        "## Requirements (open cards and 4-done)\n\n"
        "| PKG-0025-1 | MUST | x | y |  <- a\n\n"
        "## Open questions\n\n"
        "- [a] q?\n\n"
        "## Open cards (blockers first)\n\n"
        "- a\n"
        "- b\n"
    )


def test_story_table_still_defines_ids(tmp_path: Path) -> None:
    chantier = tmp_path / "_KANBAN" / "CHANTIER0025-demo"
    _card(chantier / "1-todo" / "a.md", cites="PKG-0025-7")
    (chantier / "story.md").write_text(
        "# S\n\n## Requirements\n\n| Id | P | E | C |\n|---|---|---|---|\n"
        "| PKG-0025-7 | MUST | x | y |\n",
        encoding="utf-8",
    )
    out = _run(str(chantier)).stdout
    assert "| PKG-0025-7 | MUST | x | y |  <- story.md" in out
    assert "Warning" not in out


def test_multi_line_requirements_list_is_read(tmp_path: Path) -> None:
    chantier = tmp_path / "_KANBAN" / "CHANTIER0025-demo"
    path = chantier / "1-todo" / "a.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nrequirements:\n  - PKG-0025-9\nblocked_by: null\n---\n# a\n",
        encoding="utf-8",
    )
    assert "PKG-0025-9 cited by a" in _run(str(chantier)).stdout


def test_blocking_cycle_falls_back_to_board_order(tmp_path: Path) -> None:
    chantier = tmp_path / "_KANBAN" / "CHANTIER0025-demo"
    _card(chantier / "1-todo" / "a.md", blocked_by="b")
    _card(chantier / "1-todo" / "b.md", blocked_by="a")
    out = _run(str(chantier)).stdout
    assert out.index("- a\n") < out.index("- b\n")


def test_next_id_counts_dropped_cards(tmp_path: Path) -> None:
    chantier = tmp_path / "_KANBAN" / "CHANTIER0025-demo"
    chantier.mkdir(parents=True)
    _card(
        tmp_path / "_KANBAN" / "_ARCHIVE" / "CHANTIER0025-dropped" / "2026-01-01_x.md",
        ["PKG-0025-20"],
    )
    assert _run("--next-id", "PKG", str(chantier)).stdout.strip() == "PKG-0025-21"


def test_next_id_rejects_a_bad_family(tmp_path: Path) -> None:
    chantier = tmp_path / "_KANBAN" / "CHANTIER0025-demo"
    chantier.mkdir(parents=True)
    assert _run("--next-id", "pkg", str(chantier)).returncode == 2


def _row_card(path: Path, row: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nblocked_by: null\nrequirements: []\n---\n"
        f"# {path.stem}\n\n## Requirements\n\n| Id | P | E | C |\n|---|---|---|---|\n{row}\n",
        encoding="utf-8",
    )


def test_same_id_with_identical_rows_is_listed_once(tmp_path: Path) -> None:
    chantier = tmp_path / "_KANBAN" / "CHANTIER0025-demo"
    _row_card(chantier / "1-todo" / "a.md", "| PKG-0025-1 | MUST | x | y |")
    _row_card(chantier / "3-in-progress" / "b.md", "| PKG-0025-1 | MUST | x | y |")
    assert _run(str(chantier)).stdout == (
        "## Requirements (open cards and 4-done)\n\n"
        "| PKG-0025-1 | MUST | x | y |  <- a, b\n\n"
        "## Open questions\n\n\n"
        "## Open cards (blockers first)\n\n"
        "- a\n"
        "- b\n"
    )


def test_same_id_with_different_rows_warns_and_names_the_cards(
    tmp_path: Path,
) -> None:
    chantier = tmp_path / "_KANBAN" / "CHANTIER0025-demo"
    _row_card(chantier / "1-todo" / "a.md", "| PKG-0025-1 | MUST | x | y |")
    _row_card(chantier / "1-todo" / "b.md", "| PKG-0025-1 | SHOULD | x | y |")
    _row_card(chantier / "3-in-progress" / "c.md", "| PKG-0025-1 | MUST | x | y |")
    assert _run(str(chantier)).stdout == (
        "## Requirements (open cards and 4-done)\n\n"
        "| PKG-0025-1 | MUST | x | y |  <- a, c\n"
        "| PKG-0025-1 | SHOULD | x | y |  <- b\n\n"
        "## Open questions\n\n\n"
        "## Open cards (blockers first)\n\n"
        "- a\n"
        "- b\n"
        "- c\n\n"
        "## Warning: defined with different rows\n\n"
        "- PKG-0025-1 in a, b, c\n"
    )


def test_done_cards_keep_rows_and_questions_but_are_not_open(tmp_path: Path) -> None:
    chantier = tmp_path / "_KANBAN" / "CHANTIER0025-demo"
    _card(chantier / "4-done" / "old.md", ["PKG-0025-9"], questions=["still open?"])
    _card(chantier / "1-todo" / "new.md", cites="PKG-0025-9")
    out = _run(str(chantier)).stdout
    assert "| PKG-0025-9 | MUST | x | y |  <- old" in out
    assert "- [old] still open?" in out
    open_cards = out.split("## Open cards (blockers first)")[1]
    assert "- old\n" not in open_cards
    assert "- new\n" in open_cards
    assert "Warning" not in out


def test_held_parents_section_lists_the_holding_cards(tmp_path: Path) -> None:
    chantier = tmp_path / "_KANBAN" / "CHANTIER0025-demo"
    _card(chantier / "4-done" / "parent.md")
    _card(chantier / "0-ideas" / "kid-a.md")
    (chantier / "0-ideas" / "kid-a.md").write_text(
        "---\nsert: parent\nrequirements: []\n---\n# a\n", encoding="utf-8"
    )
    _card(chantier / "1-todo" / "kid-b.md", blocked_by="parent")
    out = _run(str(chantier)).stdout
    assert "## Held parents (in 4-done, kept by open cards)" in out
    assert "- parent <- kid-a, kid-b" in out


def test_no_held_parents_section_without_open_children(tmp_path: Path) -> None:
    chantier = tmp_path / "_KANBAN" / "CHANTIER0025-demo"
    _card(chantier / "4-done" / "parent.md")
    _card(chantier / "1-todo" / "other.md")
    assert "Held parents" not in _run(str(chantier)).stdout
