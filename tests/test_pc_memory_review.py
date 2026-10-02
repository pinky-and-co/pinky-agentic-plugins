"""Tests for the pc-memory startup review reminder — run with: uv run pytest tests/test_pc_memory_review.py"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "pc-memory"
sys.path.insert(0, str(PLUGIN / "hooks" / "memory-context"))
sys.path.insert(0, str(PLUGIN / "hooks"))

import memory_context as mc  # noqa: E402
from pc_memory_lib import LEDGER_NAME, Level, read_last_review  # noqa: E402

DAY = 86400
NOW = datetime(2026, 10, 2, 12, 0).timestamp()
TODAY = date(2026, 10, 2)
STALE = 60  # above every note age used here, so no stale list gets in the way
ADVICE = (
    "Run /pc-memory:reconcile. First pick the model with /model (press s on sonnet to keep it "
    "for this session only) and the effort with /effort medium, because a skill's model override "
    "lasts a single turn; when it is done, run /clear."
)
TIP = (
    "\U0001f4a1 Tip: use /memory to enable or disable auto memory and to browse the memory "
    "entries of the current level."
)
HEAD = f"Memory by directory level, notes at least {STALE} d old listed oldest first:"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "CLAUDE_CONFIG_DIR",
        "PC_MEMORY_CONTEXT",
        "PC_MEMORY_STALE_DAYS",
        "PC_MEMORY_MODEL_LINES",
        "PC_MEMORY_REVIEW_DAYS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def home(tmp_path):
    root = tmp_path.resolve()
    (root / ".claude" / "projects").mkdir(parents=True)
    return root


def _ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


def _level(
    home: Path,
    name: str,
    notes: dict[str, int],
    ledger: object | bytes | None = None,
) -> Level:
    """A level `~/<name>` whose memory directory holds notes of the given ages (days)."""
    memory = home / "mem" / name
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("- index\n", encoding="utf-8")
    for note, age in notes.items():
        path = memory / note
        path.write_text("body\n", encoding="utf-8")
        os.utime(path, (NOW - age * DAY, NOW - age * DAY))
    if isinstance(ledger, bytes):
        (memory / LEDGER_NAME).write_bytes(ledger)
    elif ledger is not None:
        (memory / LEDGER_NAME).write_text(json.dumps(ledger), encoding="utf-8")
    return Level(root=home / name, memory_dir=memory)


def _reviewed(days: int) -> dict[str, object]:
    return {"notes": {}, "last_review": _ago(days)}


def _summary(level: Level, notes: int, role: str = "parent") -> str:
    word = "note" if notes == 1 else "notes"
    return f"> ~/{level.root.name} ({role}) - {notes} {word} - 0 at least {STALE} d old"


def _message(
    levels: list[Level], review_days: int | None = 30, tip: bool = False
) -> str:
    return mc.build_user_message(levels, levels[0].root, STALE, NOW, tip, review_days)


def _line(count: int, paths: str, days: int = 30) -> str:
    noun = "level" if count == 1 else "levels"
    return f"Review due: {count} memory {noun} not reviewed for {days}+ days ({paths}). {ADVICE}"


# --- the due rule -----------------------------------------------------------------------------


def test_review_line_is_added_when_the_last_review_is_31_days_old(home):
    a = _level(home, "a", {"n.md": 3}, _reviewed(31))
    assert _message([a]) == "\n".join(
        [HEAD, _summary(a, 1, "current level"), _line(1, "~/a")]
    )


def test_no_review_line_when_the_last_review_is_29_days_old(home):
    a = _level(home, "a", {"n.md": 3}, _reviewed(29))
    assert _message([a]) == "\n".join([HEAD, _summary(a, 1, "current level")])


def test_review_line_is_added_at_exactly_the_threshold(home):
    a = _level(home, "a", {"n.md": 3}, _reviewed(30))
    assert _message([a]) == "\n".join(
        [HEAD, _summary(a, 1, "current level"), _line(1, "~/a")]
    )


def test_a_recent_review_wins_over_old_notes(home):
    a = _level(home, "a", {"n.md": 50}, _reviewed(5))
    assert _message([a]) == "\n".join([HEAD, _summary(a, 1, "current level")])


def test_never_reviewed_level_with_a_31_day_old_note_is_due(home):
    a = _level(home, "a", {"old.md": 31, "new.md": 2})
    assert _message([a]) == "\n".join(
        [HEAD, _summary(a, 2, "current level"), _line(1, "~/a")]
    )


def test_never_reviewed_level_with_a_30_day_old_note_is_due(home):
    a = _level(home, "a", {"old.md": 30})
    assert "Review due: 1 memory level" in _message([a])


def test_never_reviewed_level_with_only_recent_notes_is_not_due(home):
    a = _level(home, "a", {"x.md": 5, "y.md": 5})
    assert _message([a]) == "\n".join([HEAD, _summary(a, 2, "current level")])


def test_a_null_last_review_counts_as_never_reviewed(home):
    due = _level(home, "a", {"old.md": 40}, {"notes": {}, "last_review": None})
    fresh = _level(home, "b", {"new.md": 4}, {"notes": {}, "last_review": None})
    assert "Review due: 1 memory level" in _message([due])
    assert "Review due" not in _message([fresh])


@pytest.mark.parametrize(
    "ledger",
    [
        b"{not json",
        b"",
        b"\xff\xfe\x00",
        b"[]",
        b'"2026-01-01"',
        b'{"last_review": "yesterday"}',
        b'{"last_review": 20260101}',
        b'{"last_review": "2026-13-45"}',
        b'{"last_review": "20260101"}',
        '{"last_review": "２０２６-０１-０１"}'.encode(),
        b'{"last_review": ["2026-01-01"]}',
        b"[" * 5000,
    ],
)
def test_a_corrupt_or_non_date_ledger_is_never_reviewed_and_left_untouched(
    home, ledger
):
    old = _level(home, "old", {"n.md": 40}, ledger)
    young = _level(home, "young", {"n.md": 4}, ledger)
    files = [old.memory_dir / LEDGER_NAME, young.memory_dir / LEDGER_NAME]
    for file in files:
        os.utime(file, (NOW - 7 * DAY, NOW - 7 * DAY))
    before = [(f.read_bytes(), f.stat().st_mtime_ns) for f in files]
    assert _message([old]) == "\n".join(
        [HEAD, _summary(old, 1, "current level"), _line(1, "~/old")]
    )
    assert "Review due" not in _message([young])
    assert [(f.read_bytes(), f.stat().st_mtime_ns) for f in files] == before
    assert not list(old.memory_dir.glob("*.corrupt"))
    assert not list(young.memory_dir.glob("*.corrupt"))


def test_a_ledger_that_is_a_directory_is_never_reviewed(home):
    a = _level(home, "a", {"n.md": 40})
    (a.memory_dir / LEDGER_NAME).mkdir()
    assert "Review due: 1 memory level" in _message([a])


def test_read_last_review_returns_the_date_or_none(home):
    ok = _level(home, "ok", {}, {"last_review": "2026-08-01", "notes": {}})
    missing = _level(home, "missing", {})
    bad = _level(home, "bad", {}, b"{oops")
    assert read_last_review(ok.memory_dir) == date(2026, 8, 1)
    assert read_last_review(missing.memory_dir) is None
    assert read_last_review(bad.memory_dir) is None
    assert read_last_review(home / "absent") is None


# --- the line ---------------------------------------------------------------------------------


def test_two_due_levels_make_one_plural_line_with_both_paths(home):
    a = _level(home, "a", {"n.md": 3}, _reviewed(40))
    b = _level(home, "b", {"n.md": 3}, _reviewed(35))
    c = _level(home, "c", {"n.md": 3}, _reviewed(2))
    assert _message([a, b, c]) == "\n".join(
        [
            HEAD,
            _summary(a, 1, "current level"),
            _summary(b, 1),
            _summary(c, 1),
            _line(2, "~/a, ~/b"),
        ]
    )


def test_four_due_levels_list_three_paths_then_one_more(home):
    levels = [_level(home, n, {"n.md": 3}, _reviewed(40)) for n in "abcd"]
    assert _message(levels) == "\n".join(
        [
            HEAD,
            _summary(levels[0], 1, "current level"),
            *(_summary(level, 1) for level in levels[1:]),
            _line(4, "~/a, ~/b, ~/c, +1 more"),
        ]
    )


def test_three_due_levels_have_no_more_marker(home):
    levels = [_level(home, n, {"n.md": 3}, _reviewed(40)) for n in "abc"]
    assert _line(3, "~/a, ~/b, ~/c") in _message(levels)


def test_the_review_line_sits_after_the_promotion_reminder_and_before_the_tip(home):
    a = _level(home, "a", {"old.md": 45}, _reviewed(40))
    assert mc.build_user_message([a], a.root, 30, NOW, True, 30) == "\n".join(
        [
            "Memory by directory level, notes at least 30 d old listed oldest first:",
            "> ~/a (current level) - 1 note - 1 at least 30 d old",
            f"  {mc.short(a.memory_dir / 'old.md')} (45 d)",
            mc.PROMOTION_REMINDER,
            _line(1, "~/a"),
            TIP,
        ]
    )


def test_the_tip_stays_last_without_a_stale_note(home):
    a = _level(home, "a", {"n.md": 3}, _reviewed(40))
    assert _message([a], tip=True) == "\n".join(
        [HEAD, _summary(a, 1, "current level"), _line(1, "~/a"), TIP]
    )


def test_the_line_has_no_control_character_even_with_one_in_a_path(home):
    a = _level(home, "a\x1b[31mred", {"n.md": 3}, _reviewed(40))
    message = _message([a])
    assert "\x1b" not in message
    assert "~/a[31mred" in message.splitlines()[-1]


def test_the_review_is_off_without_a_threshold(home):
    a = _level(home, "a", {"n.md": 3}, _reviewed(400))
    assert _message([a], review_days=None) == "\n".join(
        [HEAD, _summary(a, 1, "current level")]
    )


# --- the hook output, the environment and compaction ------------------------------------------


def _tree(home: Path, ledger: object | None, note_age: int = 3) -> Path:
    """`home/repo` with a default silo holding one note, returns the repo."""
    repo = home / "repo"
    (repo / ".git").mkdir(parents=True)
    silo = home / ".claude" / "projects" / mc.encode(repo) / "memory"
    silo.mkdir(parents=True)
    (silo / "MEMORY.md").write_text("- n\n", encoding="utf-8")
    note = silo / "n.md"
    note.write_text("body\n", encoding="utf-8")
    os.utime(note, (NOW - note_age * DAY, NOW - note_age * DAY))
    if ledger is not None:
        (silo / LEDGER_NAME).write_text(json.dumps(ledger), encoding="utf-8")
    return repo


def _startup(repo: Path, source: str = "startup") -> dict:
    return mc.build_output({"cwd": str(repo), "source": source}, NOW)


def _expected(count_line: str | None) -> str:
    lines = [
        "Memory by directory level, notes at least 30 d old listed oldest first:",
        "> ~/repo (current level) - 1 note - 0 at least 30 d old",
    ]
    if count_line:
        lines.append(count_line)
    return "\n".join([*lines, TIP])


def test_the_startup_output_carries_the_review_line_before_the_tip(home):
    repo = _tree(home, _reviewed(31))
    assert _startup(repo)["systemMessage"] == _expected(_line(1, "~/repo"))


def test_the_startup_output_has_no_review_line_for_a_recent_review(home):
    repo = _tree(home, _reviewed(29))
    assert _startup(repo)["systemMessage"] == _expected(None)


def test_the_clear_source_shows_the_line_and_compaction_shows_nothing(home):
    repo = _tree(home, _reviewed(60))
    assert "Review due" in _startup(repo, "clear")["systemMessage"]
    assert _startup(repo, "compact") == {}


def test_the_env_threshold_makes_a_15_day_old_review_due(home, monkeypatch):
    repo = _tree(home, _reviewed(15))
    assert "Review due" not in _startup(repo)["systemMessage"]
    monkeypatch.setenv("PC_MEMORY_REVIEW_DAYS", "14")
    assert _startup(repo)["systemMessage"] == _expected(_line(1, "~/repo", days=14))


def test_the_env_threshold_also_sets_the_age_of_a_note_of_a_never_reviewed_level(
    home, monkeypatch
):
    repo = _tree(home, None, note_age=15)
    assert "Review due" not in _startup(repo)["systemMessage"]
    monkeypatch.setenv("PC_MEMORY_REVIEW_DAYS", "14")
    assert (
        "Review due: 1 memory level not reviewed for 14+ days"
        in (_startup(repo)["systemMessage"])
    )


@pytest.mark.parametrize("value", ["", "abc", "0", "-5", "1.5", " "])
def test_an_invalid_threshold_falls_back_to_30(home, monkeypatch, value):
    monkeypatch.setenv("PC_MEMORY_REVIEW_DAYS", value)
    recent = _tree(home, _reviewed(15))
    assert "Review due" not in _startup(recent)["systemMessage"]
    (
        home / ".claude" / "projects" / mc.encode(recent) / "memory" / LEDGER_NAME
    ).write_text(json.dumps(_reviewed(31)), encoding="utf-8")
    assert _startup(recent)["systemMessage"] == _expected(_line(1, "~/repo"))


# --- read-only --------------------------------------------------------------------------------


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(p): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize(
    "ledger",
    [_reviewed(60), None, "corrupt"],
    ids=["old-review", "never-reviewed", "corrupt"],
)
def test_the_hook_never_writes_in_the_memory_directories(home, ledger):
    repo = _tree(home, None if ledger == "corrupt" else ledger, note_age=40)
    silo = home / ".claude" / "projects" / mc.encode(repo) / "memory"
    if ledger == "corrupt":
        (silo / LEDGER_NAME).write_bytes(b"{not json")
    before = _snapshot(home)
    out = _startup(repo)
    assert "Review due" in out["systemMessage"]
    assert _snapshot(home) == before
    assert not list(silo.glob("*.corrupt"))
