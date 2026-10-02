"""Tests for retire_requirements.py and for the two scripts that call it."""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "pc-kanban"
    / "skills"
    / "chantier"
    / "scripts"
)
HELPER = SCRIPTS / "retire_requirements.py"

TODAY = date.today().isoformat()
HEADER = "| Id | Priority | Statement | Check |\n|---|---|---|---|\n"


def rid(n: int) -> str:
    """A requirement id built from parts, so that no literal id sits in this file."""
    return "-".join(("FR", "0099", f"{n:02d}"))


def row(n: int, extra: str = "") -> str:
    return f"| {rid(n)} | MUST | statement {n}{extra} | check {n} |\n"


def story_text(rows: list[str]) -> str:
    return (
        "# Story\n\n## Requirements\n\n"
        + HEADER
        + "".join(rows)
        + "\n## Open questions\n\nnone\n"
    )


def story_lines(rows: list[str]) -> int:
    return len(story_text(rows).splitlines())


def card_text(ids: list[str], inline: bool = True) -> str:
    if inline:
        listed = f"requirements: [{', '.join(ids)}]\n"
    else:
        listed = "requirements:\n" + "".join(f"  - {i}\n" for i in ids)
    return f"---\ntitle: demo\n{listed}blocked_by: [{rid(1)}]\n---\n# Card\n"


def make_chantier(root: Path, rows: list[str]) -> Path:
    chantier = root / "_KANBAN" / "CHANTIER0099-demo"
    for column in (
        "0-ideas",
        "1-todo",
        "2-selected-for-dev",
        "3-in-progress",
        "4-done",
    ):
        (chantier / column).mkdir(parents=True)
    (chantier / "story.md").write_text(story_text(rows), encoding="utf-8")
    return chantier


def run_helper(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


def archived(root: Path, ids: list[str], inline: bool = True) -> Path:
    """A card already sitting in _ARCHIVE/, as the scripts hand it to the helper."""
    card = root / "_KANBAN" / "_ARCHIVE" / "CHANTIER0099-done" / "card.md"
    card.parent.mkdir(parents=True)
    card.write_text(card_text(ids, inline), encoding="utf-8")
    return card


def test_retires_listed_rows_and_copies_them_into_the_card(tmp_path: Path) -> None:
    chantier = make_chantier(tmp_path, [row(1), row(2), row(3)])
    card = archived(tmp_path, [rid(1), rid(3)])

    result = run_helper(card, chantier)

    assert result.returncode == 0
    assert result.stdout == (
        f"requirements: retired {rid(1)}, {rid(3)} (rows copied into card.md)\n"
        f"requirements: story.md now has {story_lines([row(2)])} lines\n"
    )
    assert (chantier / "story.md").read_text(encoding="utf-8") == story_text([row(2)])
    assert card.read_text(encoding="utf-8") == (
        card_text([rid(1), rid(3)])
        + f"\n## Requirements retired from the story ({TODAY})\n\n"
        + HEADER
        + row(1)
        + row(3)
    )


def test_keeps_a_row_carried_by_another_open_card(tmp_path: Path) -> None:
    chantier = make_chantier(tmp_path, [row(1), row(2)])
    (chantier / "1-todo" / "other.md").write_text(card_text([rid(2)]), encoding="utf-8")
    card = archived(tmp_path, [rid(1), rid(2)])

    result = run_helper(card, chantier)

    assert result.stdout == (
        f"requirements: retired {rid(1)} (rows copied into card.md)\n"
        f"requirements: kept {rid(2)} (also carried by 1-todo/other.md)\n"
        f"requirements: story.md now has {story_lines([row(2)])} lines\n"
    )
    assert (chantier / "story.md").read_text(encoding="utf-8") == story_text([row(2)])


def test_a_card_in_4_done_does_not_hold_a_row_back(tmp_path: Path) -> None:
    chantier = make_chantier(tmp_path, [row(1)])
    (chantier / "4-done" / "finished.md").write_text(
        card_text([rid(1)]), encoding="utf-8"
    )
    card = archived(tmp_path, [rid(1)])

    run_helper(card, chantier)

    assert (chantier / "story.md").read_text(encoding="utf-8") == story_text([])


def test_an_id_absent_from_the_story_is_reported_not_an_error(tmp_path: Path) -> None:
    chantier = make_chantier(tmp_path, [row(1)])
    card = archived(tmp_path, [rid(7)])

    result = run_helper(card, chantier)

    assert result.returncode == 0
    assert result.stdout == f"requirements: absent from story.md: {rid(7)}\n"
    assert (chantier / "story.md").read_text(encoding="utf-8") == story_text([row(1)])


def test_reads_the_multi_line_list_and_ignores_ids_under_other_keys(
    tmp_path: Path,
) -> None:
    chantier = make_chantier(tmp_path, [row(1), row(2)])
    card = archived(
        tmp_path, [rid(2)], inline=False
    )  # blocked_by cite rid(1), pas une exigence

    run_helper(card, chantier)

    assert (chantier / "story.md").read_text(encoding="utf-8") == story_text([row(1)])


def test_a_card_without_requirements_changes_nothing(tmp_path: Path) -> None:
    chantier = make_chantier(tmp_path, [row(1)])
    card = tmp_path / "bare.md"
    card.write_text("---\ntitle: bare\n---\n# Card\n", encoding="utf-8")

    result = run_helper(card, chantier)

    assert result.returncode == 0
    assert (
        result.stdout
        == "requirements: none listed in the card's frontmatter — nothing to retire\n"
    )
    assert (chantier / "story.md").read_text(encoding="utf-8") == story_text([row(1)])


def test_a_missing_story_is_not_an_error(tmp_path: Path) -> None:
    chantier = make_chantier(tmp_path, [row(1)])
    (chantier / "story.md").unlink()
    card = archived(tmp_path, [rid(1)])

    result = run_helper(card, chantier)

    assert result.returncode == 0
    assert result.stdout == ""
    assert "no story.md" in result.stderr


def test_a_remaining_citation_of_a_retired_id_is_reported_and_left_alone(
    tmp_path: Path,
) -> None:
    chantier = make_chantier(
        tmp_path, [row(1), row(2, extra=f" (backstop of {rid(1)})")]
    )
    card = archived(tmp_path, [rid(1)])

    result = run_helper(card, chantier)

    assert result.stdout == (
        f"requirements: retired {rid(1)} (rows copied into card.md)\n"
        f"requirements: {rid(1)} is still cited by {rid(2)} — left as is\n"
        f"requirements: story.md now has {story_lines([row(2)])} lines\n"
    )
    assert rid(1) in (chantier / "story.md").read_text(encoding="utf-8")


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    chantier = make_chantier(tmp_path, [row(1), row(2)])
    card = archived(tmp_path, [rid(1)])
    before = (
        (chantier / "story.md").read_text(encoding="utf-8"),
        card.read_text(encoding="utf-8"),
    )

    result = run_helper("--dry-run", card, chantier)

    assert result.stdout == f"requirements: would retire {rid(1)}\n"
    assert (
        (chantier / "story.md").read_text(encoding="utf-8"),
        card.read_text(encoding="utf-8"),
    ) == before


def test_a_second_run_does_not_copy_the_rows_twice(tmp_path: Path) -> None:
    chantier = make_chantier(tmp_path, [row(1)])
    card = archived(tmp_path, [rid(1)])
    run_helper(card, chantier)
    after_first = card.read_text(encoding="utf-8")

    result = run_helper(card, chantier)

    assert result.stdout == f"requirements: absent from story.md: {rid(1)}\n"
    assert card.read_text(encoding="utf-8") == after_first


def test_archive_card_moves_the_card_then_retires_its_rows(tmp_path: Path) -> None:
    chantier = make_chantier(tmp_path, [row(1), row(2)])
    card = chantier / "4-done" / "card.md"
    card.write_text(card_text([rid(1)]), encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPTS / "archive_card.sh"), str(card)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not card.exists()
    assert (
        tmp_path / "_KANBAN" / "_ARCHIVE" / "CHANTIER0099-done" / f"{TODAY}_card.md"
    ).exists()
    assert (chantier / "story.md").read_text(encoding="utf-8") == story_text([row(2)])


def _drop(chantier: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    card = chantier / "1-todo" / "idea.md"
    card.write_text(card_text([rid(1)]), encoding="utf-8")
    return subprocess.run(
        [
            "bash",
            str(SCRIPTS / "drop_card.sh"),
            *flags,
            str(card),
            "superseded, same need",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_drop_card_retires_the_rows_by_default(tmp_path: Path) -> None:
    chantier = make_chantier(tmp_path, [row(1), row(2)])

    result = _drop(chantier)

    assert result.returncode == 0, result.stderr
    assert (chantier / "story.md").read_text(encoding="utf-8") == story_text([row(2)])


def test_drop_card_keep_requirements_leaves_the_story_untouched(tmp_path: Path) -> None:
    chantier = make_chantier(tmp_path, [row(1), row(2)])

    result = _drop(chantier, "--keep-requirements")

    assert result.returncode == 0, result.stderr
    assert "requirements kept in the story (--keep-requirements)" in result.stdout
    assert (chantier / "story.md").read_text(encoding="utf-8") == story_text(
        [row(1), row(2)]
    )
