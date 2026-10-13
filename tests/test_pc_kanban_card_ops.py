"""Tests for plugins/pc-kanban/hooks/card-ops/pc_kanban_card_ops.py."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

SCRIPT = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-kanban"
    / "hooks"
    / "card-ops"
    / "pc_kanban_card_ops.py"
)


_spec = importlib.util.spec_from_file_location("card_ops", SCRIPT)
assert _spec and _spec.loader
card_ops = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(card_ops)

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


# --- archive / drop behaviour (ported from the former shell scripts) -------------------------


def _reviewed(board: Path, column: str, reviewed: bool) -> Path:
    card = _card(board, column)
    if reviewed:
        card.write_text(
            card.read_text() + "\n- [x] reviewed (n/a — mechanical)\n", encoding="utf-8"
        )
    return card


def test_archive_from_3_in_progress_when_reviewed(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    _reviewed(board, "3-in-progress", True)
    result = _run(board, "archive", "demo")
    assert result.returncode == 0, result.stderr
    assert list((board / "_ARCHIVE" / "CHANTIER0001-done").glob("*_demo.md"))


def test_archive_refuses_3_in_progress_without_reviewed_box(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    card = _reviewed(board, "3-in-progress", False)
    result = _run(board, "archive", "demo")
    assert result.returncode == 1
    assert card.is_file()
    assert "reviewed" in result.stderr


def test_archive_refuses_an_open_column(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    card = _card(board, "1-todo")
    assert _run(board, "archive", "demo").returncode != 0
    assert card.is_file()


def test_archive_and_drop_seed_no_nav_yml_and_date_the_entry(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    _card(board, "4-done")
    result = _run(board, "archive", "demo")
    folder = board / "_ARCHIVE" / "CHANTIER0001-done"
    assert not (folder / ".nav.yml").exists()
    assert not any(line.startswith("seeded") for line in result.stdout.splitlines())
    assert (
        f"archived -> {folder / (date.today().isoformat() + '_demo.md')}"
        in result.stdout.splitlines()
    )
    _card(board, "1-todo")
    dropped = _run(board, "drop", "demo", "superseded")
    dropped_folder = board / "_ARCHIVE" / "CHANTIER0001-dropped"
    assert dropped.returncode == 0, dropped.stderr
    assert dropped_folder.is_dir()
    assert not (dropped_folder / ".nav.yml").exists()
    assert not any(line.startswith("seeded") for line in dropped.stdout.splitlines())


def test_archive_refuses_to_overwrite_an_entry(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    card = _card(board, "4-done")
    folder = board / "_ARCHIVE" / "CHANTIER0001-done"
    folder.mkdir(parents=True)
    (folder / f"{date.today().isoformat()}_demo.md").write_text(
        "here", encoding="utf-8"
    )
    result = _run(board, "archive", "demo")
    assert result.returncode == 1
    assert "refusing to overwrite" in result.stderr
    assert card.is_file()


def test_drop_refuses_a_4_done_card(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    card = _card(board, "4-done")
    assert _run(board, "drop", "demo", "why").returncode != 0
    assert card.is_file()


def test_drop_refuses_an_empty_reason(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    card = _card(board, "1-todo")
    result = _run(board, "drop", "demo", "")
    assert result.returncode == 1
    assert "a reason is required" in result.stderr
    assert card.is_file()


def test_drop_appends_the_reason_and_leaves_story_untouched(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    card = _card(board, "1-todo")
    story = card.parent.parent / "story.md"
    story.write_bytes(b"| PKG-0099-1 | MUST | x | y |\n")
    assert _run(board, "drop", "demo", "bar").returncode == 0
    dropped = next((board / "_ARCHIVE" / "CHANTIER0001-dropped").glob("*_demo.md"))
    assert dropped.read_text().endswith(
        f"\n## Dropped ({date.today().isoformat()})\n\nbar\n"
    )
    assert story.read_bytes() == b"| PKG-0099-1 | MUST | x | y |\n"


# --- closing sequence (printed after archive and drop) -----------------------------------------------------


def _todo(board: Path, name: str, head: str, goal: str = "Goal of it.") -> Path:
    card = board / "CHANTIER0001-x" / "1-todo" / f"{name}.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text(f"---\n{head}---\n# T {name}\n\n{goal}\n", encoding="utf-8")
    return card


def _sequence(board: Path, signals: dict) -> str:
    return card_ops.closing_sequence(
        board / "CHANTIER0001-x", Path("_ARCHIVE/x.md"), [board], signals
    )


def test_sequence_lists_open_cards_with_goal_and_obsolescence_reminder(
    tmp_path: Path,
) -> None:
    board = tmp_path / "_KANBAN"
    _todo(board, "other", "date: 2026-01-01\n", "Why this exists.")
    out = _sequence(board, {"context_window_pct": None, "rate_limit": None})
    assert "1-todo/other.md — T other\n    Why this exists." in out
    assert "`- [x] obsolescence (none | <slug>, ...)`" in out


def test_sequence_orders_candidates_and_skips_blocked_ones(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    _todo(board, "old-p3", "date: 2026-01-01\n")
    _todo(board, "new-p3", "date: 2026-02-01\n")
    _todo(board, "p5", "date: 2026-03-01\npriority: 5\n")
    _todo(board, "blocked", "date: 2026-01-01\npriority: 5\nblocked_by: nope\n")
    _todo(board, "unblocked", "date: 2026-04-01\npriority: 1\nblocked_by: base\n")
    done = board / "_ARCHIVE" / "CHANTIER0001-done" / "2026-01-02_base.md"
    done.parent.mkdir(parents=True)
    done.write_text("x", encoding="utf-8")
    out = _sequence(board, {"context_window_pct": None, "rate_limit": None})
    picked = [line.strip()[2:] for line in out.splitlines() if line.startswith("   - ")]
    assert picked == ["p5", "old-p3", "new-p3", "unblocked"]


def test_sequence_says_nothing_to_propose_on_an_empty_board(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    (board / "CHANTIER0001-x").mkdir(parents=True)
    out = _sequence(board, {"context_window_pct": None, "rate_limit": None})
    assert "   nothing to propose" in out
    assert "(none open)" in out


def test_recommendation_on_both_sides_of_40() -> None:
    assert card_ops.recommendation({"context_window_pct": 39, "rate_limit": None}) == (
        "Reco: continue in this session (context at 39%)."
    )
    assert card_ops.recommendation({"context_window_pct": 40, "rate_limit": None}) == (
        "Reco: start a new session for the next card (context at 40%)."
    )
    assert (
        card_ops.recommendation({"context_window_pct": None, "rate_limit": None}) == ""
    )


def test_recommendation_pauses_on_a_triggered_rate_limit() -> None:
    limit = {"five_hour": {"triggered": True, "resets_at": 99, "used_percentage": 96}}
    assert card_ops.recommendation({"context_window_pct": 10, "rate_limit": limit}) == (
        "Reco: wait for the five_hour reset (resets_at 99, 96% used) before starting another card."
    )


def test_rename_step_only_from_40(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    (board / "CHANTIER0001-x").mkdir(parents=True)
    for pct, expected in ((39, False), (40, True), (75, True), (None, False)):
        out = _sequence(board, {"context_window_pct": pct, "rate_limit": None})
        assert ("/rename" in out) is expected, pct


def test_archive_cli_prints_the_sequence_after_the_unchanged_first_line(
    tmp_path: Path,
) -> None:
    board = tmp_path / "_KANBAN"
    _card(board, "4-done")
    lines = _run(board, "archive", "demo").stdout.splitlines()
    assert [line for line in lines if line.startswith("archived -> ")]
    assert "Closing sequence (finishing-a-card.md):" in lines


def test_candidate_with_a_dropped_blocker_is_kept_and_annotated(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    _todo(board, "heir", "date: 2026-01-01\nblocked_by: gone\n")
    dropped = board / "_ARCHIVE" / "CHANTIER0001-dropped" / "2026-01-02_gone.md"
    dropped.parent.mkdir(parents=True)
    dropped.write_text("x", encoding="utf-8")
    out = _sequence(board, {"context_window_pct": None, "rate_limit": None})
    assert "   - heir (blocker dropped — obsolescence check first)" in out.splitlines()


def test_candidate_without_date_is_flagged_and_sorted_last(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    _todo(board, "nodate", "priority: 5\n")
    _todo(board, "dated", "date: 2026-01-01\npriority: 1\n")
    out = _sequence(board, {"context_window_pct": None, "rate_limit": None})
    picked = [line for line in out.splitlines() if line.startswith("   - ")]
    assert picked == ["   - dated", "   - nodate (no date: — malformed)"]


def test_cli_without_a_session_id_prints_no_recommendation(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    _card(board, "4-done")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--board", str(board), "archive", "demo"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "Closing sequence (finishing-a-card.md):" in result.stdout
    assert "Reco:" not in result.stdout
    assert "/rename" not in result.stdout


def _twin(root: Path, column: str) -> tuple[Path, Path]:
    """Write `demo.md` in two chantiers of one board and return both paths."""
    first = _card(root, column)
    second = root / "CHANTIER0098-y" / column / "demo.md"
    second.parent.mkdir(parents=True)
    second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
    definer = root / "CHANTIER0098-y" / "0-ideas" / "definer.md"
    definer.parent.mkdir(parents=True, exist_ok=True)
    definer.write_text(DEFINER, encoding="utf-8")
    return first, second


def test_the_refusal_lists_each_match_as_id_slash_slug(tmp_path: Path) -> None:
    _twin(tmp_path, "1-todo")
    result = _run(tmp_path, "select", "demo")
    assert result.returncode != 0
    assert result.stderr == (
        "several cards match, pass one of these as <id>/<slug>:\n0001/demo\n0098/demo\n"
    )


def test_select_with_id_and_slug_moves_only_that_chantiers_card(
    tmp_path: Path,
) -> None:
    first, second = _twin(tmp_path, "1-todo")
    result = _run(tmp_path, "select", "0098/demo")
    assert result.returncode == 0, result.stderr
    assert first.exists()
    assert not second.exists()
    assert (tmp_path / "CHANTIER0098-y" / "2-selected-for-dev" / "demo.md").exists()


def test_archive_with_id_and_slug_archives_only_that_chantiers_card(
    tmp_path: Path,
) -> None:
    board = tmp_path / "_KANBAN"
    first, second = _twin(board, "4-done")
    result = _run(board, "archive", "0098/demo")
    assert result.returncode == 0, result.stderr
    assert first.exists()
    assert not second.exists()
    assert list((board / "_ARCHIVE" / "CHANTIER0098-done").glob("*_demo.md"))


def test_drop_with_id_and_slug_drops_only_that_chantiers_card(tmp_path: Path) -> None:
    board = tmp_path / "_KANBAN"
    first, second = _twin(board, "0-ideas")
    result = _run(board, "drop", "0098/demo", "obsolete")
    assert result.returncode == 0, result.stderr
    assert first.exists()
    assert not second.exists()


def test_an_id_with_no_such_card_is_reported(tmp_path: Path) -> None:
    _card(tmp_path, "1-todo")
    result = _run(tmp_path, "select", "0042/demo")
    assert result.returncode != 0
    assert "no card '0042/demo'" in result.stderr


def test_a_qualified_slug_with_extra_segments_is_not_cut_short(tmp_path: Path) -> None:
    card = _card(tmp_path, "1-todo", name="a")
    result = _run(tmp_path, "select", "0001/a/b")
    assert result.returncode != 0
    assert "no card '0001/a/b'" in result.stderr
    assert card.exists()


def test_a_glob_character_in_the_slug_matches_nothing_else(tmp_path: Path) -> None:
    card = _card(tmp_path, "1-todo", name="demo")
    result = _run(tmp_path, "select", "d*o")
    assert result.returncode != 0
    assert "no card 'd*o'" in result.stderr
    assert card.exists()


def _child(board: Path, column: str, head: str = "sert: demo\n") -> Path:
    return _card(board, column, "kid", head=f"model: sonnet\n{head}")


def test_archive_refuses_a_parent_held_by_an_open_child(tmp_path: Path) -> None:
    for column in ("0-ideas", "1-todo", "2-selected-for-dev", "3-in-progress"):
        for head in ("sert: demo\n", "blocked_by: demo\n"):
            board = tmp_path / f"{column}-{head.split(':')[0]}"
            parent = _card(board, "4-done")
            child = _child(board, column, head)
            result = _run(board, "archive", "demo")
            assert result.returncode == 1, result.stderr
            assert "kid" in result.stderr
            assert parent.exists() and child.exists()


def test_archive_ignores_a_child_that_is_done_or_archived(tmp_path: Path) -> None:
    board = tmp_path / "child-in-done"
    _card(board, "4-done")
    _child(board, "4-done")
    assert _run(board, "archive", "demo").returncode == 0
    for kind in ("done", "dropped"):
        board = tmp_path / kind
        _card(board, "4-done")
        archived = board / "_ARCHIVE" / f"CHANTIER0001-{kind}" / "2026-01-02_kid.md"
        archived.parent.mkdir(parents=True)
        archived.write_text("---\nsert: demo\n---\n", encoding="utf-8")
        result = _run(board, "archive", "demo")
        assert result.returncode == 0, result.stderr


def test_archive_force_passes_despite_open_children(tmp_path: Path) -> None:
    for column in ("0-ideas", "1-todo", "2-selected-for-dev", "3-in-progress"):
        board = tmp_path / column
        _card(board, "4-done")
        _child(board, column)
        result = _run(board, "archive", "demo", "--force")
        assert result.returncode == 0, result.stderr
        assert list((board / "_ARCHIVE" / "CHANTIER0001-done").glob("*_demo.md"))


def test_select_accepts_an_id_defined_only_in_a_done_card(tmp_path: Path) -> None:
    card = _card(
        tmp_path, "1-todo", head="model: sonnet\nrequirements: [PKG-0001-09]\n"
    )
    (tmp_path / "CHANTIER0001-x" / "0-ideas" / "definer.md").unlink()
    assert _run(tmp_path, "select", "demo").returncode == 1
    done = tmp_path / "CHANTIER0001-x" / "4-done" / "old.md"
    done.parent.mkdir()
    done.write_text(DEFINER.replace("PKG-0001-01", "PKG-0001-09"), encoding="utf-8")
    result = _run(tmp_path, "select", "demo")
    assert result.returncode == 0, result.stderr
    assert not card.exists()


def test_archive_refusal_wording_follows_the_card_column(tmp_path: Path) -> None:
    in_progress = tmp_path / "in-progress"
    _card(in_progress, "3-in-progress")
    _child(in_progress, "1-todo")
    refused = _run(in_progress, "archive", "demo")
    assert refused.returncode == 1
    assert "moved to 4-done/ and wait there" in refused.stderr
    done = tmp_path / "done-col"
    _card(done, "4-done")
    _child(done, "1-todo")
    assert "it waits in 4-done/" in _run(done, "archive", "demo").stderr
