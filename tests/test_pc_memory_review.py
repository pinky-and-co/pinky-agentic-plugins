"""Tests for the pc-memory startup review reminder — run with: uv run pytest tests/test_pc_memory_review.py"""

import json
import os
import sys
import unicodedata
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
DIR = "\U0001f4c2"
PIN = "\U0001f4cd"
BRANCH = "\u251c\u2500\u2500 "
LAST = "\u2514\u2500\u2500 "
HEAD = "\U0001f4da Memory"
TIP = "\U0001f4a1 Audit your memory: /memory"
SEPARATOR = " \u00b7 "
BELL = "\U0001f514 Clean it up: /pc-memory:reconcile (best on sonnet \u00b7 low effort)"


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


def _row(
    name: str, notes: int, review: str = "-", old: int = 0, depth: int = 0, pin=False
) -> str:
    """One level line of the tree, written out the way the user reads it."""
    word = "note" if notes == 1 else "notes"
    parts = [
        name,
        f"{notes} {word}",
        *([f"{old} old"] if old else []),
        f"last review: {review}",
    ]
    prefix = " " * (6 * (depth - 1)) + LAST if depth else ""
    return f"{prefix}{DIR} {SEPARATOR.join(parts)}{' ' + PIN if pin else ''}"


def _expect(*lines: str, tip: bool = False, bell: bool = False) -> str:
    """The message: the tree, then the tip, then the reminder, each part after an empty line."""
    return "\n\n".join(
        ["\n".join([HEAD, *lines]), *([TIP] if tip else []), *([BELL] if bell else [])]
    )


def _message(
    levels: list[Level],
    review_days: int | None = 30,
    tip: bool = False,
    model: object = None,
) -> str:
    return mc.build_user_message(
        levels, levels[0].root, STALE, NOW, tip, review_days, model
    )


# --- the due rule -----------------------------------------------------------------------------


def test_review_line_is_added_when_the_last_review_is_31_days_old(home):
    a = _level(home, "a", {"n.md": 3}, _reviewed(31))
    assert _message([a]) == _expect(_row("a", 1, _ago(31), pin=True), bell=True)


def test_no_review_line_when_the_last_review_is_29_days_old(home):
    a = _level(home, "a", {"n.md": 3}, _reviewed(29))
    assert _message([a]) == _expect(_row("a", 1, _ago(29), pin=True))


def test_review_line_is_added_at_exactly_the_threshold(home):
    a = _level(home, "a", {"n.md": 3}, _reviewed(30))
    assert _message([a]) == _expect(_row("a", 1, _ago(30), pin=True), bell=True)


def test_a_recent_review_wins_over_old_notes(home):
    a = _level(home, "a", {"n.md": 50}, _reviewed(5))
    assert BELL not in _message([a])


def test_never_reviewed_level_with_a_31_day_old_note_is_due(home):
    a = _level(home, "a", {"old.md": 31, "new.md": 2})
    assert _message([a]) == _expect(_row("a", 2, pin=True), bell=True)


def test_never_reviewed_level_with_a_30_day_old_note_is_due(home):
    a = _level(home, "a", {"old.md": 30})
    assert BELL in _message([a])


def test_never_reviewed_level_with_only_recent_notes_is_not_due(home):
    a = _level(home, "a", {"x.md": 5, "y.md": 5})
    assert _message([a]) == _expect(_row("a", 2, pin=True))


def test_a_null_last_review_counts_as_never_reviewed(home):
    due = _level(home, "a", {"old.md": 40}, {"notes": {}, "last_review": None})
    fresh = _level(home, "b", {"new.md": 4}, {"notes": {}, "last_review": None})
    assert BELL in _message([due])
    assert BELL not in _message([fresh])


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
    assert _message([old]) == _expect(_row("old", 1, pin=True), bell=True)
    assert BELL not in _message([young])
    assert [(f.read_bytes(), f.stat().st_mtime_ns) for f in files] == before
    assert not list(old.memory_dir.glob("*.corrupt"))
    assert not list(young.memory_dir.glob("*.corrupt"))


def test_a_ledger_that_is_a_directory_is_never_reviewed(home):
    a = _level(home, "a", {"n.md": 40})
    (a.memory_dir / LEDGER_NAME).mkdir()
    assert BELL in _message([a])


def test_read_last_review_returns_the_date_or_none(home):
    ok = _level(home, "ok", {}, {"last_review": "2026-08-01", "notes": {}})
    missing = _level(home, "missing", {})
    bad = _level(home, "bad", {}, b"{oops")
    assert read_last_review(ok.memory_dir) == date(2026, 8, 1)
    assert read_last_review(missing.memory_dir) is None
    assert read_last_review(bad.memory_dir) is None
    assert read_last_review(home / "absent") is None


# --- the line ---------------------------------------------------------------------------------


def test_the_levels_are_nested_widest_first_and_one_line_covers_every_due_level(home):
    a = _level(home, "a", {"n.md": 3}, _reviewed(40))
    b = _level(home, "b", {"n.md": 3}, _reviewed(35))
    c = _level(home, "c", {"n.md": 3}, _reviewed(2))
    assert _message([a, b, c]) == _expect(
        _row("c", 1, _ago(2)),
        _row("b", 1, _ago(35), depth=1),
        _row("a", 1, _ago(40), depth=2, pin=True),
        bell=True,
    )


def test_four_due_levels_still_make_one_line(home):
    levels = [_level(home, n, {"n.md": 3}, _reviewed(40)) for n in "abcd"]
    assert _message(levels).count(BELL) == 1
    assert _message(levels).count("\U0001f514") == 1


def test_the_bell_comes_after_the_tip(home):
    a = _level(home, "a", {"old.md": 45}, _reviewed(40))
    assert mc.build_user_message([a], a.root, 30, NOW, True, 30) == _expect(
        _row("a", 1, _ago(40), old=1, pin=True),
        LAST + mc.short(a.memory_dir / "old.md") + " (45 d)",
        tip=True,
        bell=True,
    )


def test_the_tip_alone_when_no_level_is_due(home):
    a = _level(home, "a", {"n.md": 3}, _reviewed(2))
    assert _message([a], tip=True) == _expect(_row("a", 1, _ago(2), pin=True), tip=True)


def test_a_level_name_with_a_control_character_is_cleaned(home):
    a = _level(home, "a\x1b[31mred", {"n.md": 3}, _reviewed(40))
    message = _message([a])
    assert "\x1b" not in message
    assert message.splitlines()[1] == _row("a[31mred", 1, _ago(40), pin=True)


def test_the_review_is_off_without_a_threshold(home):
    a = _level(home, "a", {"n.md": 3}, _reviewed(400))
    assert _message([a], review_days=None) == _expect(_row("a", 1, _ago(400), pin=True))


# --- the model named in the line ---------------------------------------------------------------


def _bell_for(home: Path, model: object) -> str:
    a = _level(home, "a", {"n.md": 3}, _reviewed(40))
    return _message([a], model=model).splitlines()[-1]


def test_a_model_that_is_not_a_sonnet_is_named_in_a_warning(home):
    assert _bell_for(home, "opus") == (
        "\U0001f514 Clean it up: /pc-memory:reconcile "
        "(\u26a0\ufe0f You are on opus, best on sonnet \u00b7 low effort)"
    )


@pytest.mark.parametrize(
    "model", ["claude-sonnet-5-5", "SONNET", "Claude-Sonnet-4", None, 5, ["opus"], ""]
)
def test_no_warning_for_a_sonnet_or_a_missing_or_non_string_model(home, model):
    assert _bell_for(home, model) == BELL


def test_the_model_is_cleaned_and_capped_at_40_characters(home):
    named = _bell_for(home, "op\x1b[1mus-" + "x" * 80)
    assert "\x1b" not in named
    assert named == (
        "\U0001f514 Clean it up: /pc-memory:reconcile "
        f"(\u26a0\ufe0f You are on {('op[1mus-' + 'x' * 80)[:40]}, best on sonnet \u00b7 low effort)"
    )


def test_no_line_at_all_when_no_level_is_due_whatever_the_model(home):
    a = _level(home, "a", {"n.md": 3}, _reviewed(2))
    assert "\U0001f514" not in _message([a], model="opus")


def test_every_line_has_no_control_character(home):
    a = _level(home, "a\x1b]8;;evil\x07", {"old\x1b.md": 45}, _reviewed(40))
    message = mc.build_user_message(
        [a], a.root, 30, NOW, True, 30, "o\x1bp\x00us\u202e"
    )
    for line in message.splitlines():
        assert [
            ch for ch in line if unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"}
        ] == []


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


def _expected(bell: bool, review: int | None = None) -> str:
    row = _row("repo", 1, _ago(review) if review is not None else "-", pin=True)
    return _expect(row, tip=True, bell=bell)


def test_the_startup_output_carries_the_bell_after_the_tip(home):
    repo = _tree(home, _reviewed(31))
    assert _startup(repo)["systemMessage"] == _expected(True, 31)


def test_the_startup_output_has_no_bell_for_a_recent_review(home):
    repo = _tree(home, _reviewed(29))
    assert _startup(repo)["systemMessage"] == _expected(False, 29)


def test_the_startup_output_names_the_payload_model(home):
    repo = _tree(home, _reviewed(31))
    out = mc.build_output({"cwd": str(repo), "source": "startup", "model": "opus"}, NOW)
    assert (
        out["systemMessage"]
        .splitlines()[-1]
        .startswith(
            "\U0001f514 Clean it up: /pc-memory:reconcile (\u26a0\ufe0f You are on opus,"
        )
    )


def test_the_bell_stays_when_the_tips_are_off(home):
    repo = _tree(home, _reviewed(31))
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"spinnerTipsEnabled": False}), encoding="utf-8"
    )
    assert _startup(repo)["systemMessage"] == _expect(
        _row("repo", 1, _ago(31), pin=True), bell=True
    )


def test_the_clear_source_shows_the_line_and_compaction_shows_nothing(home):
    repo = _tree(home, _reviewed(60))
    assert BELL in _startup(repo, "clear")["systemMessage"]
    assert _startup(repo, "compact") == {}


def test_the_env_threshold_makes_a_15_day_old_review_due(home, monkeypatch):
    repo = _tree(home, _reviewed(15))
    assert BELL not in _startup(repo)["systemMessage"]
    monkeypatch.setenv("PC_MEMORY_REVIEW_DAYS", "14")
    assert _startup(repo)["systemMessage"] == _expected(True, 15)


def test_the_env_threshold_also_sets_the_age_of_a_note_of_a_never_reviewed_level(
    home, monkeypatch
):
    repo = _tree(home, None, note_age=15)
    assert BELL not in _startup(repo)["systemMessage"]
    monkeypatch.setenv("PC_MEMORY_REVIEW_DAYS", "14")
    assert BELL in _startup(repo)["systemMessage"]


@pytest.mark.parametrize("value", ["", "abc", "0", "-5", "1.5", " "])
def test_an_invalid_threshold_falls_back_to_30(home, monkeypatch, value):
    monkeypatch.setenv("PC_MEMORY_REVIEW_DAYS", value)
    recent = _tree(home, _reviewed(15))
    assert BELL not in _startup(recent)["systemMessage"]
    (
        home / ".claude" / "projects" / mc.encode(recent) / "memory" / LEDGER_NAME
    ).write_text(json.dumps(_reviewed(31)), encoding="utf-8")
    assert _startup(recent)["systemMessage"] == _expected(True, 31)


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
    assert BELL in out["systemMessage"]
    assert _snapshot(home) == before
    assert not list(silo.glob("*.corrupt"))
