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


DEFINER = "# D\n\n## Requirements\n\n| Id | P | E | C |\n|---|---|---|---|\n| PKG-0001-01 | MUST | x | y |\n"


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
    definer = root / "CHANTIER0001-x" / "0-ideas" / "definer.md"
    if not definer.exists():
        definer.parent.mkdir(parents=True, exist_ok=True)
        definer.write_text(DEFINER, encoding="utf-8")
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


def test_select_refuses_an_effort_outside_the_list(tmp_path: Path) -> None:
    card = _card(
        tmp_path,
        "1-todo",
        head="model: sonnet\neffort: auto\nrequirements: [PKG-0001-01]\n",
    )
    result = _run(tmp_path, "select", "demo")
    assert result.returncode == 1
    assert "effort: auto" in result.stderr
    assert "low, medium, high, xhigh, max" in result.stderr
    assert card.exists()


def test_select_accepts_a_valid_or_absent_effort(tmp_path: Path) -> None:
    _card(
        tmp_path,
        "1-todo",
        head="model: sonnet\neffort: xhigh\nrequirements: [PKG-0001-01]\n",
    )
    assert _run(tmp_path, "select", "demo").returncode == 0
    _card(tmp_path, "1-todo", name="bare")
    assert _run(tmp_path, "select", "bare").returncode == 0


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


def test_a_reason_starting_with_a_dash_follows_a_double_dash(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    _card(board, "0-ideas")
    result = _run(board, "--", "drop", "demo", "-h")
    assert result.returncode == 0, result.stderr
    dropped = next((board / "_ARCHIVE" / "CHANTIER0001-dropped").glob("*_demo.md"))
    assert "-h" in dropped.read_text()


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
    assert "requirement PKG-0099-99 is not defined" in result.stderr
    assert card.exists()


def test_select_refuses_an_id_defined_only_in_an_archived_card(
    tmp_path: Path,
) -> None:
    card = _card(tmp_path, "1-todo", head="model: opus\nrequirements: [PKG-0001-02]\n")
    archived = tmp_path / "_ARCHIVE" / "CHANTIER0001-done" / "2026-01-02_old.md"
    archived.parent.mkdir(parents=True)
    archived.write_text(DEFINER.replace("PKG-0001-01", "PKG-0001-02"), encoding="utf-8")
    result = _run(tmp_path, "select", "demo")
    assert result.returncode == 1
    assert "PKG-0001-02 is not defined" in result.stderr
    assert card.exists()


def test_select_inserts_a_missing_date(tmp_path: Path) -> None:
    card = tmp_path / "CHANTIER0001-x" / "1-todo" / "demo.md"
    card.parent.mkdir(parents=True)
    card.write_text(
        "---\nmodel: sonnet\nrequirements: [PKG-0001-01]\n---\n## Done when\nx\n",
        encoding="utf-8",
    )
    (tmp_path / "CHANTIER0001-x" / "0-ideas").mkdir(parents=True, exist_ok=True)
    (tmp_path / "CHANTIER0001-x" / "0-ideas" / "definer.md").write_text(
        DEFINER, encoding="utf-8"
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


def test_select_accepts_an_id_still_defined_in_the_story(tmp_path: Path) -> None:
    _card(tmp_path, "1-todo", head="model: sonnet\nrequirements: [PKG-0001-05]\n")
    (tmp_path / "CHANTIER0001-x" / "story.md").write_text(
        DEFINER.replace("PKG-0001-01", "PKG-0001-05"), encoding="utf-8"
    )
    result = _run(tmp_path, "select", "demo")
    assert result.returncode == 0, result.stderr


def _mockup_head(extra: str) -> str:
    return f"model: sonnet\n{extra}requirements: [PKG-0001-01]\n"


def test_select_accepts_a_valid_mockup_and_fidelity(tmp_path: Path) -> None:
    mock = tmp_path / "poc.html"
    mock.write_text("<p>x</p>")
    _card(
        tmp_path,
        "1-todo",
        head=_mockup_head(
            f"mockup: {mock}\nmockup_fidelity: [layout=strict, design=free]  # c\n"
        ),
    )
    assert _run(tmp_path, "select", "demo").returncode == 0


def test_select_accepts_a_mockup_without_fidelity_and_a_tilde_path(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    (home / "m").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _card(tmp_path, "1-todo", head=_mockup_head("mockup: ~/m\n"))
    assert _run(tmp_path, "select", "demo").returncode == 0


def test_select_refuses_a_mockup_path_that_does_not_exist(tmp_path: Path) -> None:
    card = _card(
        tmp_path, "1-todo", head=_mockup_head(f"mockup: {tmp_path}/absent.html\n")
    )
    result = _run(tmp_path, "select", "demo")
    assert result.returncode == 1
    assert "absent.html" in result.stderr
    assert card.exists()


def test_select_refuses_a_relative_mockup_path(tmp_path: Path) -> None:
    (tmp_path / "poc.html").write_text("x")
    _card(tmp_path, "1-todo", head=_mockup_head("mockup: poc.html\n"))
    result = _run(tmp_path, "select", "demo")
    assert result.returncode == 1
    assert "absolute" in result.stderr


def test_select_refuses_an_unknown_axis_naming_the_values(tmp_path: Path) -> None:
    mock = tmp_path / "poc.html"
    mock.write_text("x")
    _card(
        tmp_path,
        "1-todo",
        head=_mockup_head(f"mockup: {mock}\nmockup_fidelity: [color=strict]\n"),
    )
    result = _run(tmp_path, "select", "demo")
    assert result.returncode == 1
    assert "color" in result.stderr
    assert "layout, design, content, behavior" in result.stderr


def test_select_refuses_an_unknown_level_naming_the_values(tmp_path: Path) -> None:
    mock = tmp_path / "poc.html"
    mock.write_text("x")
    _card(
        tmp_path,
        "1-todo",
        head=_mockup_head(f"mockup: {mock}\nmockup_fidelity: [layout=exact]\n"),
    )
    result = _run(tmp_path, "select", "demo")
    assert result.returncode == 1
    assert "exact" in result.stderr
    assert "strict, guided, free" in result.stderr


def test_select_refuses_a_fidelity_element_without_equals(tmp_path: Path) -> None:
    mock = tmp_path / "poc.html"
    mock.write_text("x")
    _card(
        tmp_path,
        "1-todo",
        head=_mockup_head(f"mockup: {mock}\nmockup_fidelity: [layout]\n"),
    )
    result = _run(tmp_path, "select", "demo")
    assert result.returncode == 1
    assert "axe=niveau" in result.stderr


def test_select_refuses_a_fidelity_without_mockup(tmp_path: Path) -> None:
    card = _card(
        tmp_path, "1-todo", head=_mockup_head("mockup_fidelity: [layout=strict]\n")
    )
    result = _run(tmp_path, "select", "demo")
    assert result.returncode == 1
    assert "mockup_fidelity" in result.stderr
    assert "`mockup:`" in result.stderr
    assert card.exists()


def _fidelity_card(tmp_path: Path, fidelity: str) -> subprocess.CompletedProcess[str]:
    mock = tmp_path / "poc.html"
    mock.write_text("x")
    _card(
        tmp_path,
        "1-todo",
        head=_mockup_head(f"mockup: {mock}\nmockup_fidelity: {fidelity}\n"),
    )
    return _run(tmp_path, "select", "demo")


def test_select_accepts_spaces_around_the_equals_sign(tmp_path: Path) -> None:
    assert _fidelity_card(tmp_path, "[layout = strict]").returncode == 0


def test_select_refuses_an_unbracketed_fidelity_naming_the_form(tmp_path: Path) -> None:
    result = _fidelity_card(tmp_path, "layout=strict")
    assert result.returncode == 1
    assert "[layout=strict, design=free]" in result.stderr


def test_select_refuses_a_duplicate_axis_naming_it(tmp_path: Path) -> None:
    result = _fidelity_card(tmp_path, "[layout=strict, layout=free]")
    assert result.returncode == 1
    assert "layout" in result.stderr
    assert "twice" in result.stderr


def test_select_refuses_a_block_style_fidelity_list(tmp_path: Path) -> None:
    result = _fidelity_card(tmp_path, "\n  - layout=strict")
    assert result.returncode == 1
    assert "[layout=strict, design=free]" in result.stderr


def test_select_refuses_a_capitalised_axis(tmp_path: Path) -> None:
    result = _fidelity_card(tmp_path, "[Layout=strict]")
    assert result.returncode == 1
    assert "Layout" in result.stderr


def test_select_accepts_a_directory_as_mockup(tmp_path: Path) -> None:
    folder = tmp_path / "poc"
    folder.mkdir()
    _card(tmp_path, "1-todo", head=_mockup_head(f"mockup: {folder}\n"))
    assert _run(tmp_path, "select", "demo").returncode == 0
