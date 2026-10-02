"""Tests for the pc-memory `reconcile` scripts — run with: uv run pytest tests/test_pc_memory_reconcile.py"""

import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "pc-memory"
SCRIPTS = PLUGIN / "skills" / "reconcile" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(PLUGIN / "hooks"))

import apply as ap  # noqa: E402
import inventory as inv  # noqa: E402
from pc_memory_lib import LEDGER_NAME, LOCK_NAME, MARKER_NAME, encode  # noqa: E402

TODAY = date(2026, 10, 2)
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No ambient setting leaks into a test, nor into the subprocess."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Fake home with a `.claude/projects`, set as `HOME`."""
    root = tmp_path.resolve()
    (root / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(root))
    return root


def _silo(home: Path, level: Path) -> Path:
    directory = home / ".claude" / "projects" / encode(level) / "memory"
    directory.mkdir(parents=True)
    return directory


def _short(path: Path, home: Path) -> str:
    return "~" + str(path)[len(str(home)) :]


def _note(
    directory: Path,
    name: str,
    body: str,
    modified: str | None = None,
    description: str | None = None,
    kind: str | None = None,
) -> None:
    head = []
    if description is not None:
        head.append(f"description: {description}")
    if kind is not None:
        head.append(f"type: {kind}")
    if modified is not None:
        head.append(f"modified: {modified}")
    front = "---\n" + "\n".join(head) + "\n---\n" if head else ""
    (directory / name).write_text(front + body, encoding="utf-8")


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _snapshot(*roots: Path) -> dict[str, bytes | None]:
    """Every path under the roots with its content (None for a directory)."""
    out: dict[str, bytes | None] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            out[str(path)] = path.read_bytes() if path.is_file() else None
    return out


# --- inventory ----------------------------------------------------------------------


@pytest.fixture
def tree(home):
    """Three levels: home/org/ws/repo (git root, current), home/org/ws, home/org."""
    org = home / "org"
    ws = org / "ws"
    repo = ws / "repo"
    (repo / ".git").mkdir(parents=True)
    current, parent, grand = _silo(home, repo), _silo(home, ws), _silo(home, org)

    (current / "MEMORY.md").write_text(
        "# Memory\n"
        "- [Build trap](build_trap.md) - local trap\n"
        "- [Tone](tone.md) - how to talk\n"
        "- [Ghost](ghost.md) - file removed\n"
        "- [Dup](dup.md) - same as the parent one\n",
        encoding="utf-8",
    )
    _note(
        current,
        "build_trap.md",
        "Run the build twice.\n",
        modified="2026-09-02T12:00:00Z",
        description="Build is flaky on first run",
        kind="feedback",
    )
    _note(current, "tone.md", "Be terse.\n")
    mtime = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc).timestamp()
    os.utime(current / "tone.md", (mtime, mtime))
    _note(
        current,
        "dup.md",
        "Same   body\n\nacross levels.\n",
        modified="2026-10-02T11:00:00Z",
    )
    _note(current, "loose.md", "Nobody lists me.\n", modified="2026-10-01T12:00:00Z")
    (current / LEDGER_NAME).write_text(
        json.dumps(
            {
                "notes": {
                    "build_trap.md": {
                        "sessions": 3,
                        "last_session": "s3",
                        "last_read": "2026-10-01",
                    },
                    "gone.md": {
                        "sessions": 9,
                        "last_session": "x",
                        "last_read": "2026-01-01",
                    },
                },
                "last_review": "2026-08-01",
            }
        ),
        encoding="utf-8",
    )
    (current / LOCK_NAME).write_text("", encoding="utf-8")
    (current / (LEDGER_NAME + ".corrupt")).write_text("{bad", encoding="utf-8")
    (current / ".pc-memory-ledger.abc123").write_text("{}", encoding="utf-8")
    (current / ".hidden.md").write_text("hidden", encoding="utf-8")
    (current / "notes.txt").write_text("not a note", encoding="utf-8")
    trash = current / ".reconcile-trash" / "2026-09-01"
    trash.mkdir(parents=True)
    (trash / "old.md").write_text("trashed", encoding="utf-8")

    (parent / "MEMORY.md").write_text(
        "- [Shared dup](dup_shared.md) - shared\n", encoding="utf-8"
    )
    _note(
        parent,
        "dup_shared.md",
        "Same body across levels.",
        modified="2025-10-02T12:00:00Z",
    )

    filler = "".join(f"filler line {n}\n" for n in range(200))
    (grand / "MEMORY.md").write_text(
        filler + "- [Only](only.md) - the only note\n", encoding="utf-8"
    )
    _note(grand, "only.md", "Only here.\n", modified="2026-10-02T12:00:00Z")
    return {
        "repo": repo,
        "ws": ws,
        "org": org,
        "current": current,
        "parent": parent,
        "grand": grand,
    }


def test_inventory_of_a_three_level_tree(home, tree):
    index_size = (tree["grand"] / "MEMORY.md").stat().st_size
    expected = {
        "levels": [
            {
                "level": _short(tree["repo"], home),
                "dir": _short(tree["current"], home),
                "role": "current",
                "last_review": "2026-08-01",
                "index": {
                    "lines": 5,
                    "bytes": (tree["current"] / "MEMORY.md").stat().st_size,
                    "over_budget": False,
                },
                "notes": [
                    {
                        "file": "build_trap.md",
                        "description": "Build is flaky on first run",
                        "type": "feedback",
                        "age_days": 30,
                        "sessions": 3,
                        "last_read": "2026-10-01",
                        "in_index": True,
                        "index_line": "- [Build trap](build_trap.md) - local trap",
                        "coverage": [],
                    },
                    {
                        "file": "dup.md",
                        "description": "",
                        "type": "",
                        "age_days": 0,
                        "sessions": 0,
                        "last_read": None,
                        "in_index": True,
                        "index_line": "- [Dup](dup.md) - same as the parent one",
                        "coverage": [],
                    },
                    {
                        "file": "loose.md",
                        "description": "",
                        "type": "",
                        "age_days": 1,
                        "sessions": 0,
                        "last_read": None,
                        "in_index": False,
                        "index_line": None,
                        "coverage": [],
                    },
                    {
                        "file": "tone.md",
                        "description": "",
                        "type": "",
                        "age_days": 5,
                        "sessions": 0,
                        "last_read": None,
                        "in_index": True,
                        "index_line": "- [Tone](tone.md) - how to talk",
                        "coverage": [],
                    },
                ],
                "orphans": {
                    "index_without_file": ["ghost.md"],
                    "file_without_index": ["loose.md"],
                },
            },
            {
                "level": _short(tree["ws"], home),
                "dir": _short(tree["parent"], home),
                "role": "parent",
                "last_review": None,
                "index": {
                    "lines": 1,
                    "bytes": (tree["parent"] / "MEMORY.md").stat().st_size,
                    "over_budget": False,
                },
                "notes": [
                    {
                        "file": "dup_shared.md",
                        "description": "",
                        "type": "",
                        "age_days": 365,
                        "sessions": 0,
                        "last_read": None,
                        "in_index": True,
                        "index_line": "- [Shared dup](dup_shared.md) - shared",
                        "coverage": [],
                    }
                ],
                "orphans": {"index_without_file": [], "file_without_index": []},
            },
            {
                "level": _short(tree["org"], home),
                "dir": _short(tree["grand"], home),
                "role": "parent",
                "last_review": None,
                "index": {"lines": 201, "bytes": index_size, "over_budget": True},
                "notes": [
                    {
                        "file": "only.md",
                        "description": "",
                        "type": "",
                        "age_days": 0,
                        "sessions": 0,
                        "last_read": None,
                        "in_index": True,
                        "index_line": "- [Only](only.md) - the only note",
                        "coverage": [],
                    }
                ],
                "orphans": {"index_without_file": [], "file_without_index": []},
            },
        ],
        "duplicates": {
            "exact": [
                [
                    {"level": _short(tree["ws"], home), "file": "dup_shared.md"},
                    {"level": _short(tree["repo"], home), "file": "dup.md"},
                ]
            ]
        },
    }
    assert inv.build_inventory(tree["repo"], NOW) == expected


def test_inventory_never_writes_not_even_a_corrupt_ledger(home, tree):
    (tree["current"] / LEDGER_NAME).write_text("{not json", encoding="utf-8")
    before = _snapshot(home / ".claude")
    report = inv.build_inventory(tree["repo"], NOW)
    assert _snapshot(home / ".claude") == before
    assert report["levels"][0]["last_review"] is None
    assert report["levels"][0]["notes"][0]["sessions"] == 0


def test_index_budget_is_200_lines_or_25_kb(home, tree):
    index = tree["parent"] / "MEMORY.md"
    index.write_text("- [a](dup_shared.md)\n" + "x" * 25 * 1024, encoding="utf-8")
    assert inv.build_inventory(tree["repo"], NOW)["levels"][1]["index"]["over_budget"]
    index.write_text("- [a](dup_shared.md)\n" + "x" * 1000, encoding="utf-8")
    assert not inv.build_inventory(tree["repo"], NOW)["levels"][1]["index"][
        "over_budget"
    ]
    index.write_text("\n" * 200, encoding="utf-8")
    assert not inv.build_inventory(tree["repo"], NOW)["levels"][1]["index"][
        "over_budget"
    ]
    index.write_text("\n" * 201, encoding="utf-8")
    assert inv.build_inventory(tree["repo"], NOW)["levels"][1]["index"]["over_budget"]


def test_inventory_text_from_notes_is_cleaned(home, tree):
    _note(
        tree["current"],
        "evil.md",
        "x",
        modified="2026-10-02T12:00:00Z",
        description="line\x1b[31m red‮ end",
    )
    notes = inv.build_inventory(tree["repo"], NOW)["levels"][0]["notes"]
    evil = next(n for n in notes if n["file"] == "evil.md")
    assert evil["description"] == "line[31m red end"


def test_inventory_cli_prints_json_for_cwd(home, tree):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "inventory.py"), "--cwd", str(tree["repo"])],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
        check=False,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert [lv["role"] for lv in data["levels"]] == ["current", "parent", "parent"]


def test_duplicates_are_found_inside_a_level_too(home, tree):
    _note(tree["current"], "dup2.md", "Same body\nacross levels.\n\n\n")
    _note(tree["current"], "other.md", "same body across levels\n")
    exact = inv.build_inventory(tree["repo"], NOW)["duplicates"]["exact"]
    assert exact == [
        [
            {"level": _short(tree["ws"], home), "file": "dup_shared.md"},
            {"level": _short(tree["repo"], home), "file": "dup.md"},
            {"level": _short(tree["repo"], home), "file": "dup2.md"},
        ]
    ]


# --- apply --------------------------------------------------------------------------


@pytest.fixture
def two(home):
    """A current level and a parent level with notes, indexes and ledgers."""
    ws = home / "ws"
    repo = ws / "repo"
    (repo / ".git").mkdir(parents=True)
    cur, par = _silo(home, repo), _silo(home, ws)
    (cur / "MEMORY.md").write_text(
        "# Memory\n- [A](a.md) - first\n- [B](b.md) - second\n- [C](c.md) - third\n",
        encoding="utf-8",
    )
    for name in ("a", "b", "c"):
        _note(cur, f"{name}.md", f"body {name}\n", description=f"desc {name}")
    (par / "MEMORY.md").write_text("- [P](p.md) - parent note\n", encoding="utf-8")
    _note(par, "p.md", "parent body\n")
    ledger = {
        "notes": {
            n: {"sessions": 2, "last_session": "s", "last_read": "2026-10-01"}
            for n in ("a", "b", "c")
        },
        "last_review": None,
    }
    ledger["notes"] = {f"{k}.md": v for k, v in ledger["notes"].items()}
    (cur / LEDGER_NAME).write_text(json.dumps(ledger), encoding="utf-8")
    return {"repo": repo, "ws": ws, "cur": cur, "par": par}


def _apply(two, actions, reviewed=(), dry_run=False):
    plan = ap.parse_plan(
        {"actions": actions, "reviewed_dirs": [str(d) for d in reviewed]}, two["repo"]
    )
    return ap.apply_plan(plan, TODAY, dry_run)


def _act(directory, note, action, **extra):
    out = {"dir": str(directory), "note": note, "action": action, **extra}
    return {k: str(v) if isinstance(v, Path) else v for k, v in out.items()}


def test_delete_goes_to_trash_and_cleans_the_index_and_the_ledger(home, two):
    cur = two["cur"]
    summary = _apply(two, [_act(cur, "a.md", "delete")], reviewed=[cur])
    short_cur = _short(cur, home)
    assert summary == {
        "dry_run": False,
        "ok": True,
        "results": [
            {
                "dir": short_cur,
                "note": "a.md",
                "action": "delete",
                "hard": False,
                "trash": f"{short_cur}/.reconcile-trash/2026-10-02/a.md",
                "status": "done",
                "index_lines_removed": 1,
                "ledger_entry_pruned": True,
            }
        ],
        "last_review": {short_cur: "2026-10-02"},
    }
    assert not (cur / "a.md").exists()
    assert (cur / ".reconcile-trash" / "2026-10-02" / "a.md").read_text(
        encoding="utf-8"
    ) == ("---\ndescription: desc a\n---\nbody a\n")
    assert (cur / "MEMORY.md").read_text(encoding="utf-8") == (
        "# Memory\n- [B](b.md) - second\n- [C](c.md) - third\n"
    )
    assert sorted(_json(cur / LEDGER_NAME)["notes"]) == ["b.md", "c.md"]
    assert not (cur / MARKER_NAME).exists()


def test_two_deletes_of_the_same_name_keep_both_in_the_trash(home, two):
    cur = two["cur"]
    _apply(two, [_act(cur, "a.md", "delete")])
    _note(cur, "a.md", "second a\n")
    _apply(two, [_act(cur, "a.md", "delete")])
    day = cur / ".reconcile-trash" / "2026-10-02"
    assert sorted(p.name for p in day.iterdir()) == ["a.md", "a.md.1"]


def test_hard_delete_leaves_no_trash(home, two):
    cur = two["cur"]
    summary = _apply(two, [_act(cur, "b.md", "delete", hard=True)])
    assert not (cur / "b.md").exists()
    assert not (cur / ".reconcile-trash").exists()
    assert "trash" not in summary["results"][0]
    assert summary["results"][0]["hard"] is True
    assert "b.md" not in (cur / "MEMORY.md").read_text(encoding="utf-8")


def test_move_across_levels_fixes_both_indexes(home, two):
    cur, par = two["cur"], two["par"]
    summary = _apply(two, [_act(cur, "c.md", "move", to_dir=par)])
    assert summary["results"][0]["status"] == "done"
    assert summary["results"][0]["to_dir"] == _short(par, home)
    assert not (cur / "c.md").exists()
    assert (par / "c.md").read_text(
        encoding="utf-8"
    ) == "---\ndescription: desc c\n---\nbody c\n"
    assert (cur / "MEMORY.md").read_text(encoding="utf-8") == (
        "# Memory\n- [A](a.md) - first\n- [B](b.md) - second\n"
    )
    assert (par / "MEMORY.md").read_text(encoding="utf-8") == (
        "- [P](p.md) - parent note\n- [C](c.md) - third\n"
    )
    assert "c.md" not in _json(cur / LEDGER_NAME)["notes"]
    assert not (par / MARKER_NAME).exists() and not (cur / MARKER_NAME).exists()


def test_move_of_a_note_without_index_line_makes_one(home, two):
    cur, par = two["cur"], two["par"]
    _note(cur, "loose.md", "x\n", description="Loose one")
    _apply(two, [_act(cur, "loose.md", "move", to_dir=par)])
    assert (par / "MEMORY.md").read_text(encoding="utf-8").splitlines()[-1] == (
        "- [loose](loose.md) - Loose one"
    )


def test_keep_changes_nothing(home, two):
    (two["cur"] / LOCK_NAME).write_text("", encoding="utf-8")
    before = _snapshot(two["cur"], two["par"])
    summary = _apply(two, [_act(two["cur"], "a.md", "keep")])
    assert summary["results"][0]["status"] == "kept"
    assert _snapshot(two["cur"], two["par"]) == before


def test_last_review_is_set_only_for_reviewed_dirs(home, two):
    cur, par = two["cur"], two["par"]
    _apply(two, [_act(cur, "a.md", "keep"), _act(par, "p.md", "keep")], reviewed=[par])
    assert _json(par / LEDGER_NAME)["last_review"] == "2026-10-02"
    assert _json(cur / LEDGER_NAME)["last_review"] is None


def test_last_review_keeps_the_rest_of_the_ledger(home, two):
    cur = two["cur"]
    before = _json(cur / LEDGER_NAME)
    _apply(two, [], reviewed=[cur])
    after = _json(cur / LEDGER_NAME)
    assert after["notes"] == before["notes"]
    assert after["last_review"] == "2026-10-02"


def test_marker_exists_during_the_run_and_the_lock_is_taken_after_it(
    home, two, monkeypatch
):
    cur, par = two["cur"], two["par"]
    seen = []
    real_lock, real_remove = ap.exclusive_lock, ap._remove_index_lines

    def lock(directory, timeout, message):
        seen.append(
            ("lock", (cur / MARKER_NAME).exists(), (par / MARKER_NAME).exists())
        )
        return real_lock(directory, timeout, message)

    def remove(directory, note, removable):
        seen.append(
            ("action", (cur / MARKER_NAME).exists(), (par / MARKER_NAME).exists())
        )
        return real_remove(directory, note, removable)

    monkeypatch.setattr(ap, "exclusive_lock", lock)
    monkeypatch.setattr(ap, "_remove_index_lines", remove)
    _apply(two, [_act(cur, "c.md", "move", to_dir=par)])
    assert seen == [("lock", True, True), ("lock", True, True), ("action", True, True)]
    assert not (cur / MARKER_NAME).exists() and not (par / MARKER_NAME).exists()


def test_marker_is_removed_after_a_failure_halfway(home, two, monkeypatch):
    cur, par = two["cur"], two["par"]
    real = ap._remove_index_lines
    calls = []

    def remove(directory, note, removable):
        calls.append(note)
        if note == "b.md":
            raise OSError("disk full")
        return real(directory, note, removable)

    monkeypatch.setattr(ap, "_remove_index_lines", remove)
    summary = _apply(
        two,
        [
            _act(cur, "a.md", "delete"),
            _act(cur, "b.md", "delete"),
            _act(cur, "c.md", "delete"),
        ],
        reviewed=[cur, par],
    )
    assert summary["ok"] is False
    assert summary["error"] == "OSError: disk full"
    assert [r["note"] for r in summary["results"]] == ["a.md", "b.md"]
    assert summary["results"][1]["status"] == "failed"
    assert summary["results"][1]["steps"] == ["file"]
    assert summary["left"] == [
        {"dir": _short(cur, home), "note": "c.md", "action": "delete"},
    ]
    assert not (cur / MARKER_NAME).exists() and not (par / MARKER_NAME).exists()
    assert _json(cur / LEDGER_NAME)["last_review"] is None


def test_marker_is_removed_after_a_failure_creating_the_next_marker(
    home, two, monkeypatch
):
    cur, par = two["cur"], two["par"]
    real_open = os.open

    def opener(path, flags, mode=0o777):
        if Path(path) == par / MARKER_NAME:
            raise OSError("no space")
        return real_open(path, flags, mode)

    monkeypatch.setattr(ap.os, "open", opener)
    summary = _apply(two, [_act(cur, "a.md", "delete")], reviewed=[par])
    monkeypatch.undo()
    assert summary["ok"] is False
    assert (cur / "a.md").exists()
    assert not (cur / MARKER_NAME).exists()


@pytest.mark.parametrize(
    "bad",
    [
        {"dir": "OUTSIDE", "note": "a.md", "action": "delete"},
        {"dir": "CUR", "note": "a.md", "action": "move", "to_dir": "OUTSIDE"},
        {"dir": "CUR", "note": "a.md", "action": "move"},
        {"dir": "CUR", "note": "a.md", "action": "move", "to_dir": "CUR"},
        {"dir": "CUR", "note": "../a.md", "action": "delete"},
        {"dir": "CUR", "note": "sub/a.md", "action": "delete"},
        {"dir": "CUR", "note": "MEMORY.md", "action": "delete"},
        {"dir": "CUR", "note": ".hidden.md", "action": "delete"},
        {"dir": "CUR", "note": "a.txt", "action": "delete"},
        {"dir": "CUR", "note": "a.md", "action": "shred"},
        {"dir": "CUR", "note": "a.md", "action": "delete", "hard": "yes"},
        {"dir": "relative/dir", "note": "a.md", "action": "delete"},
        {"dir": "CUR", "note": 3, "action": "delete"},
    ],
)
def test_a_bad_action_refuses_the_whole_plan_and_changes_nothing(
    home, two, tmp_path, bad
):
    outside = tmp_path / "outside"
    outside.mkdir()
    fixed = {
        k: (str(two["cur"]) if v == "CUR" else str(outside) if v == "OUTSIDE" else v)
        for k, v in bad.items()
    }
    plan = {
        "actions": [_act(two["cur"], "a.md", "delete"), fixed],
        "reviewed_dirs": [str(two["cur"])],
    }
    before = _snapshot(home / ".claude", outside)
    with pytest.raises(ap.PlanError):
        ap.parse_plan(plan, two["repo"])
    assert _snapshot(home / ".claude", outside) == before


def test_a_reviewed_dir_that_is_not_a_memory_dir_is_refused(home, two, tmp_path):
    with pytest.raises(ap.PlanError, match="not the memory directory"):
        ap.parse_plan({"actions": [], "reviewed_dirs": [str(tmp_path)]}, two["repo"])


def test_a_move_onto_an_existing_note_is_refused(home, two):
    _note(two["par"], "c.md", "already there\n")
    with pytest.raises(ap.PlanError, match="differ"):
        ap.parse_plan(
            {"actions": [_act(two["cur"], "c.md", "move", to_dir=two["par"])]},
            two["repo"],
        )


def test_a_declared_directory_of_a_level_is_a_memory_dir(home, two):
    declared = home / "declared"
    declared.mkdir()
    (declared / "MEMORY.md").write_text("", encoding="utf-8")
    (two["repo"] / ".claude").mkdir()
    (two["repo"] / ".claude" / "settings.local.json").write_text(
        json.dumps({"autoMemoryDirectory": str(declared)}), encoding="utf-8"
    )
    plan = ap.parse_plan({"actions": [_act(declared, "x.md", "keep")]}, two["repo"])
    assert plan.directories() == [declared]


def test_a_directory_declared_in_shared_project_settings_is_refused(home, two):
    declared = home / "declared"
    declared.mkdir()
    (two["repo"] / ".claude").mkdir()
    (two["repo"] / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": str(declared)}), encoding="utf-8"
    )
    with pytest.raises(ap.PlanError, match="not the memory directory"):
        ap.parse_plan({"actions": [_act(declared, "x.md", "keep")]}, two["repo"])


def test_an_existing_marker_refuses_the_plan_and_is_left_alone(home, two):
    (two["cur"] / MARKER_NAME).write_text("", encoding="utf-8")
    with pytest.raises(
        ap.PlanError, match=r"another reconcile runs \(marker \d+ s old\)"
    ):
        ap.parse_plan({"actions": [_act(two["cur"], "a.md", "delete")]}, two["repo"])
    assert (two["cur"] / MARKER_NAME).exists()


def test_tilde_directories_are_expanded(home, two):
    plan = ap.parse_plan(
        {"actions": [_act(_short(two["cur"], home), "a.md", "keep")]}, two["repo"]
    )
    assert plan.directories() == [two["cur"]]


def test_dry_run_changes_nothing_not_even_the_marker(home, two, monkeypatch):
    cur, par = two["cur"], two["par"]

    def forbidden(*args, **kwargs):
        raise AssertionError("dry run touched the file system")

    monkeypatch.setattr(ap.os, "open", forbidden)
    monkeypatch.setattr(ap, "exclusive_lock", forbidden)
    before = _snapshot(home / ".claude")
    summary = _apply(
        two,
        [
            _act(cur, "a.md", "delete"),
            _act(cur, "c.md", "move", to_dir=par),
            _act(cur, "ghost.md", "delete", hard=True),
        ],
        reviewed=[cur],
        dry_run=True,
    )
    assert _snapshot(home / ".claude") == before
    short_cur = _short(cur, home)
    assert summary == {
        "dry_run": True,
        "ok": True,
        "results": [
            {
                "dir": short_cur,
                "note": "a.md",
                "action": "delete",
                "to_dir": None,
                "hard": False,
                "status": "planned",
            },
            {
                "dir": short_cur,
                "note": "c.md",
                "action": "move",
                "to_dir": _short(par, home),
                "hard": False,
                "status": "planned",
            },
            {
                "dir": short_cur,
                "note": "ghost.md",
                "action": "delete",
                "to_dir": None,
                "hard": True,
                "status": "already_gone",
            },
        ],
        "reviewed_dirs": [short_cur],
    }


def test_the_same_plan_twice_is_safe(home, two):
    cur, par = two["cur"], two["par"]
    actions = [
        _act(cur, "a.md", "delete"),
        _act(cur, "b.md", "delete", hard=True),
        _act(cur, "c.md", "move", to_dir=par),
    ]
    first = _apply(two, actions, reviewed=[cur])
    after_first = _snapshot(home / ".claude")
    second = _apply(two, actions, reviewed=[cur])
    assert [r["status"] for r in first["results"]] == ["done", "done", "done"]
    assert second["ok"] is True
    assert [r["status"] for r in second["results"]] == ["already_gone"] * 3
    assert [r["index_lines_removed"] for r in second["results"]] == [0, 0, 0]
    assert _snapshot(home / ".claude") == after_first


def test_cli_exit_codes_and_summary(home, two, tmp_path):
    plan = tmp_path / "plan.json"
    env = {**os.environ, "HOME": str(home)}

    def run(*extra):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "apply.py"),
                "--plan",
                "-",
                "--cwd",
                str(two["repo"]),
                *extra,
            ],
            input=plan.read_text(encoding="utf-8"),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    plan.write_text(
        json.dumps({"actions": [_act(two["cur"], "a.md", "delete")]}), encoding="utf-8"
    )
    dry = run("--dry-run")
    assert (dry.returncode, json.loads(dry.stdout)["dry_run"]) == (0, True)
    assert (two["cur"] / "a.md").exists()
    done = run()
    assert (done.returncode, json.loads(done.stdout)["ok"]) == (0, True)
    assert not (two["cur"] / "a.md").exists()

    plan.write_text(
        json.dumps({"actions": [_act(two["cur"], "../x.md", "delete")]}),
        encoding="utf-8",
    )
    refused = run()
    assert refused.returncode == 2
    assert json.loads(refused.stdout)["ok"] is False

    plan.write_text(
        json.dumps(
            {
                "actions": [
                    _act(two["cur"], "b.md", "delete"),
                    _act(two["cur"], "../x.md", "delete"),
                ]
            }
        ),
        encoding="utf-8",
    )
    assert run().returncode == 2
    assert (two["cur"] / "b.md").exists()
    assert not (two["cur"] / MARKER_NAME).exists()

    plan.write_text("{nope", encoding="utf-8")
    assert run().returncode == 2


def test_lock_held_by_someone_else_fails_and_removes_the_marker(home, two, monkeypatch):
    import fcntl

    cur = two["cur"]
    monkeypatch.setattr(ap, "LOCK_TIMEOUT", 0.1)
    with open(cur / LOCK_NAME, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        summary = _apply(two, [_act(cur, "a.md", "delete")])
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    assert summary["ok"] is False
    assert (
        summary["error"]
        == f"TimeoutError: ledger lock of {_short(cur, home)} still held"
    )
    assert (cur / "a.md").exists()
    assert not (cur / MARKER_NAME).exists()


@pytest.mark.parametrize(
    ("line", "targets"),
    [
        ("- [A](a.md) - hook", ["a.md"]),
        ("- [A](./a.md#top) and [B](sub/b.md)", ["a.md", "sub/b.md"]),
        ("- [A b](a%20b.md)", ["a%20b.md", "a b.md"]),
        ("- [site](https://example.com/x.md) [mail](mailto:a@b.md)", []),
        ("plain text, no link", []),
    ],
)
def test_link_targets_of_an_index_line(line, targets):
    from pc_memory_lib import link_targets

    assert link_targets(line) == targets


def test_a_note_with_a_space_in_its_name_is_found_in_the_index(home, two):
    cur = two["cur"]
    _note(cur, "my note.md", "x\n")
    with (cur / "MEMORY.md").open("a", encoding="utf-8") as handle:
        handle.write("- [Mine](my%20note.md) - spaced\n")
    summary = _apply(two, [_act(cur, "my note.md", "delete")])
    assert summary["results"][0]["index_lines_removed"] == 1
    assert "my" not in (cur / "MEMORY.md").read_text(encoding="utf-8")


# --- review round: index protection -------------------------------------------------

sys.path.insert(0, str(PLUGIN / "hooks" / "memory-count"))
import errno  # noqa: E402
import signal  # noqa: E402
import time  # noqa: E402
from urllib.parse import quote  # noqa: E402

import memory_count as cnt  # noqa: E402
from pc_memory_lib import MARKER_MAX_AGE, marker_active  # noqa: E402


@pytest.fixture
def three(home, two):
    """`two` plus a grandparent level, and an `x.md` in the current and the parent level."""
    org = home / "org"
    repo = org / "ws" / "repo"
    (repo / ".git").mkdir(parents=True)
    cur, par, grand = _silo(home, repo), _silo(home, org / "ws"), _silo(home, org)
    for directory in (cur, par, grand):
        (directory / "MEMORY.md").write_text("# M\n", encoding="utf-8")
    for directory in (cur, par):
        _note(directory, "x.md", f"x of {directory.parent.name}\n")
        with (directory / "MEMORY.md").open("a", encoding="utf-8") as handle:
            handle.write("- [X](x.md) - x\n")
    return {"repo": repo, "cur": cur, "par": par, "grand": grand}


@pytest.mark.parametrize("action", ["delete", "hard", "move"])
def test_the_index_is_protected_whatever_its_case(home, two, action):
    extra = {"to_dir": two["par"]} if action == "move" else {}
    item = _act(
        two["cur"],
        "memory.md",
        "delete" if action == "hard" else action,
        **extra,
        **({"hard": True} if action == "hard" else {}),
    )
    before = _snapshot(home / ".claude")
    with pytest.raises(ap.PlanError, match="not a plain"):
        ap.parse_plan({"actions": [item]}, two["repo"])
    assert _snapshot(home / ".claude") == before


@pytest.mark.parametrize("action", ["delete", "hard", "move"])
def test_a_link_to_the_index_is_refused_when_the_plan_is_read(home, two, action):
    (two["cur"] / "alias.md").symlink_to(two["cur"] / "MEMORY.md")
    extra = {"to_dir": two["par"]} if action == "move" else {}
    item = _act(
        two["cur"],
        "alias.md",
        "delete" if action == "hard" else action,
        **extra,
        **({"hard": True} if action == "hard" else {}),
    )
    with pytest.raises(ap.PlanError, match="is the index"):
        ap.parse_plan({"actions": [item]}, two["repo"])


def test_a_link_to_the_index_made_after_validation_is_refused_when_applying(home, two):
    plan = ap.parse_plan(
        {"actions": [_act(two["cur"], "alias.md", "delete", hard=True)]}, two["repo"]
    )
    index = (two["cur"] / "MEMORY.md").read_bytes()
    (two["cur"] / "alias.md").symlink_to(two["cur"] / "MEMORY.md")
    summary = ap.apply_plan(plan, TODAY)
    assert summary["ok"] is False
    assert "is the index" in summary["error"]
    assert (two["cur"] / "MEMORY.md").read_bytes() == index
    assert not (two["cur"] / MARKER_NAME).exists()


# --- review round: a move never overwrites ------------------------------------------


def test_two_moves_to_the_same_destination_are_refused(home, three):
    plan = {
        "actions": [
            _act(three["cur"], "x.md", "move", to_dir=three["grand"]),
            _act(three["par"], "x.md", "move", to_dir=three["grand"]),
        ]
    }
    before = _snapshot(home / ".claude")
    with pytest.raises(ap.PlanError, match="destination of another move"):
        ap.parse_plan(plan, three["repo"])
    assert _snapshot(home / ".claude") == before


def test_two_actions_on_the_same_source_are_refused(home, two):
    plan = {
        "actions": [
            _act(two["cur"], "a.md", "delete"),
            _act(two["cur"], "A.md", "keep"),
        ]
    }
    with pytest.raises(ap.PlanError, match="source of another action"):
        ap.parse_plan(plan, two["repo"])


def test_a_destination_that_appears_after_validation_is_never_overwritten(home, two):
    cur, par = two["cur"], two["par"]
    plan = ap.parse_plan(
        {"actions": [_act(cur, "c.md", "move", to_dir=par)]}, two["repo"]
    )
    (par / "c.md").write_text("someone else's\n", encoding="utf-8")
    before_index = (par / "MEMORY.md").read_bytes()
    summary = ap.apply_plan(plan, TODAY)
    assert summary["ok"] is False
    assert summary["error"].startswith("FileExistsError")
    assert (par / "c.md").read_text(encoding="utf-8") == "someone else's\n"
    assert (cur / "c.md").exists()
    assert (par / "MEMORY.md").read_bytes() == before_index
    assert "c.md" in (cur / "MEMORY.md").read_text(encoding="utf-8")
    assert not (cur / MARKER_NAME).exists()


def test_a_move_between_volumes_still_works(home, two, monkeypatch):
    cur, par = two["cur"], two["par"]
    real_replace, real_link = os.replace, os.link

    def across(real):
        def guarded(src, dst, *args, **kwargs):
            if os.path.dirname(src) != os.path.dirname(dst):
                raise OSError(errno.EXDEV, "cross-device link")
            return real(src, dst, *args, **kwargs)

        return guarded

    monkeypatch.setattr(os, "replace", across(real_replace))
    monkeypatch.setattr(os, "link", across(real_link))
    summary = _apply(two, [_act(cur, "c.md", "move", to_dir=par)])
    monkeypatch.undo()
    assert summary["ok"] is True
    assert not (cur / "c.md").exists()
    assert (par / "c.md").read_text(encoding="utf-8") == (
        "---\ndescription: desc c\n---\nbody c\n"
    )


def test_a_symlink_note_is_moved_as_a_link(home, two, tmp_path):
    cur, par = two["cur"], two["par"]
    target = tmp_path / "elsewhere.txt"
    target.write_text("data\n", encoding="utf-8")
    (cur / "link.md").symlink_to(target)
    _apply(two, [_act(cur, "link.md", "move", to_dir=par)])
    assert not os.path.lexists(cur / "link.md")
    assert (par / "link.md").is_symlink()
    assert os.readlink(par / "link.md") == str(target)
    assert target.read_text(encoding="utf-8") == "data\n"


def test_a_folder_named_like_a_note_is_refused_and_ignored_by_the_inventory(home, tree):
    (tree["current"] / "x.md").mkdir()
    level = inv.build_inventory(tree["repo"], NOW)["levels"][0]
    assert level["dir"] == _short(tree["current"], home)
    assert [n["file"] for n in level["notes"]] == [
        "build_trap.md",
        "dup.md",
        "loose.md",
        "tone.md",
    ]
    with pytest.raises(ap.PlanError, match="neither a regular file nor a symlink"):
        ap.parse_plan(
            {"actions": [_act(tree["current"], "x.md", "delete")]}, tree["repo"]
        )


# --- review round: the marker -------------------------------------------------------


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def test_marker_active_needs_a_fresh_regular_file(home, two, tmp_path):
    cur = two["cur"]
    assert not marker_active(cur)
    marker = cur / MARKER_NAME
    marker.write_text("", encoding="utf-8")
    assert marker_active(cur)
    _age(marker, MARKER_MAX_AGE - 5)
    assert marker_active(cur)
    _age(marker, MARKER_MAX_AGE + 5)
    assert not marker_active(cur)
    marker.unlink()
    marker.symlink_to(tmp_path)
    assert not marker_active(cur)
    marker.unlink()
    marker.mkdir()
    assert not marker_active(cur)


def _read_payload(two, session):
    return {
        "tool_name": "Read",
        "session_id": session,
        "cwd": str(two["repo"]),
        "tool_input": {"file_path": str(two["cur"] / "a.md")},
    }


def test_the_counter_ignores_a_marker_of_11_minutes(home, two):
    marker = two["cur"] / MARKER_NAME
    marker.write_text("", encoding="utf-8")
    assert cnt.record_read(_read_payload(two, "s1"), TODAY) is False
    _age(marker, 11 * 60)
    assert cnt.record_read(_read_payload(two, "s2"), TODAY) is True


def test_a_marker_of_11_minutes_does_not_block_and_is_removed_by_the_run(home, two):
    marker = two["cur"] / MARKER_NAME
    marker.write_text("", encoding="utf-8")
    _age(marker, 11 * 60)
    summary = _apply(two, [_act(two["cur"], "a.md", "delete")])
    assert summary["ok"] is True
    assert not marker.exists()
    assert not (two["cur"] / "a.md").exists()


def test_a_marker_that_is_not_a_file_refuses_the_plan(home, two):
    (two["cur"] / MARKER_NAME).mkdir()
    with pytest.raises(ap.PlanError, match="not a regular file"):
        ap.parse_plan({"actions": [_act(two["cur"], "a.md", "delete")]}, two["repo"])


def test_the_marker_is_refreshed_before_each_action(home, two, monkeypatch):
    cur = two["cur"]
    marker = cur / MARKER_NAME
    real = ap._apply_action
    ages = []

    def probe(*args, **kwargs):
        ages.append(time.time() - marker.stat().st_mtime)
        result = real(*args, **kwargs)
        _age(marker, 300)
        return result

    monkeypatch.setattr(ap, "_apply_action", probe)
    _apply(two, [_act(cur, "a.md", "delete"), _act(cur, "b.md", "delete")])
    assert len(ages) == 2 and max(ages) < 60


def test_two_applications_at_once_one_is_refused_and_does_not_touch_the_marker(
    home, two, monkeypatch
):
    cur = two["cur"]
    first = ap.parse_plan({"actions": [_act(cur, "a.md", "delete")]}, two["repo"])
    second = ap.parse_plan({"actions": [_act(cur, "b.md", "delete")]}, two["repo"])
    real = ap._remove_index_lines
    seen = {}

    def remove(directory, note, removable):
        if "second" not in seen:
            seen["second"] = ap.apply_plan(second, TODAY)
            seen["marker_after_second"] = (cur / MARKER_NAME).exists()
        return real(directory, note, removable)

    monkeypatch.setattr(ap, "_remove_index_lines", remove)
    summary = ap.apply_plan(first, TODAY)
    assert summary["ok"] is True
    assert seen["second"]["ok"] is False
    assert seen["second"]["error"].startswith("FileExistsError")
    assert seen["marker_after_second"] is True
    assert (cur / "b.md").exists()
    assert not (cur / MARKER_NAME).exists()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_a_killed_run_removes_its_markers(home, two, sig):
    import fcntl

    cur = two["cur"]
    plan = json.dumps({"actions": [_act(cur, "a.md", "delete")]})
    with open(cur / LOCK_NAME, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        proc = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPTS / "apply.py"),
                "--plan",
                "-",
                "--cwd",
                str(two["repo"]),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, "HOME": str(home)},
        )
        proc.stdin.write(plan)
        proc.stdin.close()
        deadline = time.monotonic() + 10
        while not (cur / MARKER_NAME).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (cur / MARKER_NAME).exists()
        proc.send_signal(sig)
        code = proc.wait(timeout=10)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    assert code == 128 + sig
    assert not (cur / MARKER_NAME).exists()
    assert (cur / "a.md").exists()


# --- review round: move order and replay --------------------------------------------


def test_a_move_interrupted_after_the_file_is_replayed_to_consistent_indexes(
    home, two, monkeypatch
):
    cur, par = two["cur"], two["par"]
    actions = [_act(cur, "c.md", "move", to_dir=par)]
    real = ap._remove_index_lines
    monkeypatch.setattr(
        ap,
        "_remove_index_lines",
        lambda *a: (_ for _ in ()).throw(OSError("killed")),
    )
    summary = _apply(two, actions)
    monkeypatch.setattr(ap, "_remove_index_lines", real)
    assert summary["ok"] is False
    assert summary["results"] == [
        {
            "dir": _short(cur, home),
            "note": "c.md",
            "action": "move",
            "to_dir": _short(par, home),
            "status": "failed",
            "steps": ["destination_index", "file"],
        }
    ]
    assert summary["left"] == []
    assert (par / "c.md").exists() and not (cur / "c.md").exists()
    assert "(c.md)" in (par / "MEMORY.md").read_text(encoding="utf-8")
    assert "(c.md)" in (cur / "MEMORY.md").read_text(encoding="utf-8")

    again = _apply(two, actions)
    assert again["ok"] is True
    assert again["results"][0]["status"] == "already_gone"
    assert (cur / "MEMORY.md").read_text(encoding="utf-8") == (
        "# Memory\n- [A](a.md) - first\n- [B](b.md) - second\n"
    )
    assert (par / "MEMORY.md").read_text(encoding="utf-8") == (
        "- [P](p.md) - parent note\n- [C](c.md) - third\n"
    )
    assert "c.md" not in _json(cur / LEDGER_NAME)["notes"]


def test_a_note_already_at_the_destination_gets_its_missing_index_line(home, two):
    cur, par = two["cur"], two["par"]
    os.replace(cur / "c.md", par / "c.md")
    _apply(two, [_act(cur, "c.md", "move", to_dir=par)])
    assert (par / "MEMORY.md").read_text(encoding="utf-8") == (
        "- [P](p.md) - parent note\n- [C](c.md) - third\n"
    )
    assert "c.md" not in (cur / "MEMORY.md").read_text(encoding="utf-8")


def test_the_destination_index_gets_a_newline_and_never_a_duplicate(home, two):
    cur, par = two["cur"], two["par"]
    (par / "MEMORY.md").write_text("- [P](p.md) - parent note", encoding="utf-8")
    _apply(two, [_act(cur, "c.md", "move", to_dir=par)])
    expected = "- [P](p.md) - parent note\n- [C](c.md) - third\n"
    assert (par / "MEMORY.md").read_text(encoding="utf-8") == expected
    assert ap._append_index_lines(par, "c.md", ["- [C](c.md) - third"]) is False
    assert (par / "MEMORY.md").read_text(encoding="utf-8") == expected


# --- review round: shared index lines -----------------------------------------------


def _share(directory: Path) -> str:
    line = "- [A and B](a.md) and [B](b.md) - together"
    with (directory / "MEMORY.md").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return line


def test_a_line_that_cites_two_notes_stays_when_only_one_is_deleted(home, two):
    cur = two["cur"]
    line = _share(cur)
    summary = _apply(two, [_act(cur, "a.md", "delete")])
    assert summary["results"][0]["shared_index_line"] == [line]
    assert summary["results"][0]["index_lines_removed"] == 1
    text = (cur / "MEMORY.md").read_text(encoding="utf-8")
    assert line in text and "- [A](a.md) - first" not in text


def test_a_line_that_cites_two_notes_goes_when_the_plan_takes_out_both(home, two):
    cur = two["cur"]
    line = _share(cur)
    summary = _apply(two, [_act(cur, "a.md", "delete"), _act(cur, "b.md", "delete")])
    assert all("shared_index_line" not in r for r in summary["results"])
    assert line not in (cur / "MEMORY.md").read_text(encoding="utf-8")


def test_a_moved_note_of_a_shared_line_gets_its_own_line_at_the_destination(home, two):
    cur, par = two["cur"], two["par"]
    (cur / "MEMORY.md").write_text("- [A](a.md) and [B](b.md)\n", encoding="utf-8")
    summary = _apply(two, [_act(cur, "a.md", "move", to_dir=par)])
    assert summary["results"][0]["shared_index_line"] == ["- [A](a.md) and [B](b.md)"]
    assert (par / "MEMORY.md").read_text(encoding="utf-8") == (
        "- [P](p.md) - parent note\n- [a](a.md) - desc a\n"
    )
    assert (cur / "MEMORY.md").read_text(encoding="utf-8") == (
        "- [A](a.md) and [B](b.md)\n"
    )


# --- review round: inventory and apply see the same levels -------------------------


def test_a_directory_declared_only_in_the_shared_settings_is_neither_listed_nor_accepted(
    home, two
):
    declared = home / "declared"
    declared.mkdir()
    (declared / "MEMORY.md").write_text("- [Z](z.md)\n", encoding="utf-8")
    (two["repo"] / ".claude").mkdir()
    (two["repo"] / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": str(declared)}), encoding="utf-8"
    )
    dirs = [lv["dir"] for lv in inv.build_inventory(two["repo"], NOW)["levels"]]
    assert dirs == [_short(two["cur"], home), _short(two["par"], home)]
    with pytest.raises(ap.PlanError, match="not the memory directory"):
        ap.parse_plan({"actions": [_act(declared, "z.md", "keep")]}, two["repo"])


def test_a_directory_declared_in_the_local_settings_is_listed_and_accepted(home, two):
    declared = home / "declared"
    declared.mkdir()
    (declared / "MEMORY.md").write_text("- [Z](z.md)\n", encoding="utf-8")
    (two["repo"] / ".claude").mkdir()
    (two["repo"] / ".claude" / "settings.local.json").write_text(
        json.dumps({"autoMemoryDirectory": str(declared)}), encoding="utf-8"
    )
    assert inv.build_inventory(two["repo"], NOW)["levels"][0]["dir"] == _short(
        declared, home
    )
    assert ap.parse_plan({"actions": [_act(declared, "z.md", "keep")]}, two["repo"])


def test_the_silo_of_another_project_is_refused(home, two):
    other = home / ".claude" / "projects" / "-some-other-project" / "memory"
    other.mkdir(parents=True)
    (other / "MEMORY.md").write_text("", encoding="utf-8")
    with pytest.raises(ap.PlanError, match="not the memory directory"):
        ap.parse_plan({"actions": [_act(other, "z.md", "keep")]}, two["repo"])
    with pytest.raises(ap.PlanError, match="not the memory directory"):
        ap.parse_plan({"actions": [], "reviewed_dirs": [str(other)]}, two["repo"])


# --- review round: skill file -------------------------------------------------------


def test_the_skill_grants_no_read_or_write_and_passes_the_plan_on_stdin():
    text = (PLUGIN / "skills" / "reconcile" / "SKILL.md").read_text(encoding="utf-8")
    front = text.split("---\n")[1]
    tools = next(
        ln for ln in front.splitlines() if ln.startswith("allowed-tools:")
    ).split(":", 1)[1]
    names = {part.strip().split("(")[0] for part in tools.split(", ")}
    assert names == {"Bash", "AskUserQuestion"}
    assert "apply.py" not in tools
    assert "--plan -" in text
    assert "--dry-run" in text
    assert "permanent deletion" in text


# --- review round: surviving mutations ----------------------------------------------


def test_the_budget_boundary_is_25_kb_exactly(home, tree):
    index = tree["parent"] / "MEMORY.md"
    index.write_text("x" * (25 * 1024), encoding="utf-8")
    assert not inv.build_inventory(tree["repo"], NOW)["levels"][1]["index"][
        "over_budget"
    ]
    index.write_text("x" * (25 * 1024 + 1), encoding="utf-8")
    assert inv.build_inventory(tree["repo"], NOW)["levels"][1]["index"]["over_budget"]


def test_notes_with_an_empty_body_are_not_duplicates(home, tree):
    _note(tree["current"], "e1.md", "", description="one")
    _note(tree["current"], "e2.md", "  \n", description="two")
    (tree["current"] / "e3.md").write_text("", encoding="utf-8")
    exact = inv.build_inventory(tree["repo"], NOW)["duplicates"]["exact"]
    assert [m["file"] for group in exact for m in group] == ["dup_shared.md", "dup.md"]


def test_orphans_are_index_links_to_missing_notes_only(home, tree):
    current = tree["current"]
    _note(current, "a b.md", "spaced\n")
    (current / "MEMORY.md").write_text(
        "- [ok](build_trap.md)\n"
        "- [sp](a%20b.md)\n"
        "- [url](https://example.com/z.md)\n"
        "- [sub](sub/q.md)\n"
        "- [txt](notes.txt)\n"
        "- [self](MEMORY.md)\n"
        "- [lower](memory.md)\n",
        encoding="utf-8",
    )
    orphans = inv.build_inventory(tree["repo"], NOW)["levels"][0]["orphans"]
    assert orphans["index_without_file"] == ["sub/q.md"]
    in_index = {
        n["file"]: n["in_index"]
        for n in inv.build_inventory(tree["repo"], NOW)["levels"][0]["notes"]
    }
    assert in_index["a b.md"] is True


def test_an_index_line_is_capped_at_200_characters(home, tree):
    long = "- [Build trap](build_trap.md) " + "y" * 300
    (tree["current"] / "MEMORY.md").write_text(long + "\n", encoding="utf-8")
    note = inv.build_inventory(tree["repo"], NOW)["levels"][0]["notes"][0]
    assert note["index_line"] == long[:200]


def test_a_description_is_unquoted_and_its_spaces_normalised(home, tree):
    (tree["current"] / "tone.md").write_text(
        '---\ndescription: "  spaced   out\ttext  "\n---\nbody\n', encoding="utf-8"
    )
    notes = inv.build_inventory(tree["repo"], NOW)["levels"][0]["notes"]
    assert next(n for n in notes if n["file"] == "tone.md")["description"] == (
        "spaced out text"
    )


def test_an_unreadable_level_is_skipped_and_the_others_kept(home, tree, capsys):
    index = tree["parent"] / "MEMORY.md"
    index.chmod(0)
    try:
        if os.access(index, os.R_OK):
            pytest.skip("permissions are not enforced for this user")
        report = inv.build_inventory(tree["repo"], NOW)
    finally:
        index.chmod(0o644)
    assert [lv["dir"] for lv in report["levels"]] == [
        _short(tree["current"], home),
        _short(tree["grand"], home),
    ]
    assert "inventory:" in capsys.readouterr().err


def test_a_failed_run_exits_1_and_sets_no_last_review(home, two, monkeypatch, capsys):
    cur = two["cur"]
    plan_text = json.dumps(
        {"actions": [_act(cur, "a.md", "delete")], "reviewed_dirs": [str(cur)]}
    )
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(plan_text))
    monkeypatch.setattr(
        sys, "argv", ["apply.py", "--plan", "-", "--cwd", str(two["repo"])]
    )
    monkeypatch.setattr(
        ap, "_remove_index_lines", lambda *a: (_ for _ in ()).throw(OSError("boom"))
    )
    assert ap.main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["last_review"] == {}
    assert _json(cur / LEDGER_NAME)["last_review"] is None
    assert not (cur / MARKER_NAME).exists()


def test_a_real_pass_touches_nothing_outside_the_memory_directories(home, two):
    outside = home / "ws"
    (outside / "notes.txt").write_text("keep me\n", encoding="utf-8")
    before = {k: v for k, v in _snapshot(home).items() if "/.claude/projects/" not in k}
    cur, par = two["cur"], two["par"]
    _apply(
        two,
        [
            _act(cur, "a.md", "delete"),
            _act(cur, "b.md", "delete", hard=True),
            _act(cur, "c.md", "move", to_dir=par),
        ],
        reviewed=[cur, par],
    )
    after = {k: v for k, v in _snapshot(home).items() if "/.claude/projects/" not in k}
    assert after == before


def test_summary_text_that_comes_from_the_index_is_cleaned(home, two):
    cur = two["cur"]
    (cur / "MEMORY.md").write_text(
        "- [A](a.md) and [B](b.md) \x1b[31mred‮\n", encoding="utf-8"
    )
    summary = _apply(two, [_act(cur, "a.md", "delete")])
    assert summary["results"][0]["shared_index_line"] == [
        "- [A](a.md) and [B](b.md) [31mred"
    ]


def test_the_write_guard_does_not_see_the_apply_command(home, two):
    sys.path.insert(0, str(PLUGIN / "hooks" / "memory-write"))
    import memory_write_guard as mw

    command = (
        f"python3 {SCRIPTS}/apply.py --plan - <<'EOF'\n"
        + json.dumps({"actions": [_act(two["cur"], "a.md", "delete")]})
        + "\nEOF"
    )
    payload = {
        "tool_name": "Bash",
        "cwd": str(two["repo"]),
        "tool_input": {"command": command},
    }
    assert mw.build_output(payload) == {}


def test_the_exclusive_copy_never_replaces_an_existing_destination(tmp_path):
    source, dest = tmp_path / "s.md", tmp_path / "d.md"
    source.write_text("mine\n", encoding="utf-8")
    dest.write_text("theirs\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        ap._copy_exclusive(source, dest)
    assert dest.read_text(encoding="utf-8") == "theirs\n"
    assert source.read_text(encoding="utf-8") == "mine\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["d.md", "s.md"]


def test_the_copy_in_place_fallback_never_replaces_a_destination_either(
    tmp_path, monkeypatch
):
    source, dest = tmp_path / "s.md", tmp_path / "d.md"
    source.write_text("mine\n", encoding="utf-8")
    dest.write_text("theirs\n", encoding="utf-8")

    def refuse(*args, **kwargs):
        raise OSError(errno.EPERM, "no hard link here")

    monkeypatch.setattr(os, "link", refuse)
    with pytest.raises(FileExistsError):
        ap._copy_exclusive(source, dest)
    monkeypatch.undo()
    assert dest.read_text(encoding="utf-8") == "theirs\n"


def test_a_copy_that_fails_midway_leaves_no_half_file(tmp_path, monkeypatch):
    source, dest = tmp_path / "s.md", tmp_path / "d.md"
    source.write_text("mine\n", encoding="utf-8")

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ap.shutil, "copyfileobj", broken)
    with pytest.raises(OSError, match="disk full"):
        ap._copy_exclusive(source, dest)
    monkeypatch.undo()
    assert source.exists() and not dest.exists()


# --- review round 3: crash-safe move ------------------------------------------------

DRIVER = r"""
import json, os, sys
from datetime import date
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import apply as ap
crash_at = int(os.environ["CRASH_AT"])
calls = [0]
def wrap(name):
    real = getattr(os, name)
    def guarded(*a, **k):
        calls[0] += 1
        if calls[0] == crash_at:
            os._exit(75)
        return real(*a, **k)
    setattr(os, name, guarded)
for name in ("open", "replace", "link", "unlink", "symlink", "rename", "utime", "mkdir", "chmod"):
    wrap(name)
if os.environ.get("CRASH_MID_COPY"):
    def half(src, dst, *a):
        dst.write(src.read(1))
        dst.flush()
        os._exit(76)
    ap.shutil.copyfileobj = half
plan = ap.parse_plan(json.load(open(sys.argv[2])), Path(sys.argv[3]))
ap.apply_plan(plan, date(2026, 10, 2))
print(calls[0])
"""
CRASH_MAX = 90
TEMP_NAME = re.compile(
    r"^\.pc-memory-(move|index|ledger)\.[a-z0-9_]{8}$", re.IGNORECASE
)


def _crash_run(home, two, plan, crash_at, **env):
    plan_file = home / "crash-plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-c", DRIVER, str(SCRIPTS), str(plan_file), str(two["repo"])],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home), "CRASH_AT": str(crash_at), **env},
        check=False,
    )


def _crash_plan(two):
    return {
        "actions": [
            _act(two["cur"], "a.md", "delete"),
            _act(two["cur"], "c.md", "move", to_dir=two["par"]),
        ],
        "reviewed_dirs": [str(two["cur"])],
    }


def _age_markers(two):
    for directory in (two["cur"], two["par"]):
        if (directory / MARKER_NAME).exists():
            _age(directory / MARKER_NAME, 11 * 60)


def _assert_converged(two, orig):
    cur, par = two["cur"], two["par"]
    assert not (cur / "c.md").exists()
    assert (par / "c.md").read_bytes() == orig
    assert (par / "MEMORY.md").read_text(encoding="utf-8").count("(c.md)") == 1
    assert "c.md" not in (cur / "MEMORY.md").read_text(encoding="utf-8")
    assert "a.md" not in (cur / "MEMORY.md").read_text(encoding="utf-8")
    assert not (cur / "a.md").exists()
    trashed = list((cur / ".reconcile-trash" / "2026-10-02").iterdir())
    assert [t.name for t in trashed] == ["a.md"]
    notes = _json(cur / LEDGER_NAME)["notes"]
    assert "a.md" not in notes and "c.md" not in notes
    assert _json(cur / LEDGER_NAME)["last_review"] == "2026-10-02"
    for directory in (cur, par):
        assert not [p for p in directory.iterdir() if TEMP_NAME.match(p.name)]
        assert not (directory / MARKER_NAME).exists()


def test_the_crash_driver_reaches_the_end_of_a_clean_run(home, two):
    run = _crash_run(home, two, _crash_plan(two), 0)
    assert run.returncode == 0, run.stderr
    assert int(run.stdout.strip()) <= CRASH_MAX


@pytest.mark.parametrize("crash_at", range(1, CRASH_MAX + 1))
def test_a_run_killed_before_any_os_call_converges_when_the_plan_is_replayed(
    home, two, crash_at
):
    orig = (two["cur"] / "c.md").read_bytes()
    run = _crash_run(home, two, _crash_plan(two), crash_at)
    assert run.returncode in (0, 75), run.stderr
    cur, par = two["cur"], two["par"]
    assert (cur / "c.md").exists() or (par / "c.md").exists()
    if (par / "c.md").exists():
        assert (par / "c.md").read_bytes() == orig
    _age_markers(two)
    summary = _apply(two, _crash_plan(two)["actions"], reviewed=[cur])
    assert summary["ok"] is True
    _assert_converged(two, orig)


def test_a_run_killed_during_the_copy_converges_when_the_plan_is_replayed(home, two):
    cur, par = two["cur"], two["par"]
    (cur / "c.md").write_text("x" * 5000, encoding="utf-8")
    orig = (cur / "c.md").read_bytes()
    run = _crash_run(home, two, _crash_plan(two), 0, CRASH_MID_COPY="1")
    assert run.returncode == 76, run.stderr
    assert not (par / "c.md").exists()
    assert [p.name for p in par.iterdir() if TEMP_NAME.match(p.name)]
    _age_markers(two)
    assert _apply(two, _crash_plan(two)["actions"], reviewed=[cur])["ok"] is True
    _assert_converged(two, orig)


def test_a_destination_is_never_visible_half_written(home, two, monkeypatch):
    cur, par = two["cur"], two["par"]
    seen = []

    def half(src, dst, *args):
        dst.write(src.read(1))
        seen.append(sorted(p.name for p in par.iterdir() if p.name == "c.md"))
        raise OSError("disk full")

    monkeypatch.setattr(ap.shutil, "copyfileobj", half)
    summary = _apply(two, [_act(cur, "c.md", "move", to_dir=par)])
    monkeypatch.undo()
    assert summary["ok"] is False
    assert seen == [[]]
    assert (cur / "c.md").exists() and not (par / "c.md").exists()
    assert not [p for p in par.iterdir() if TEMP_NAME.match(p.name)]


@pytest.mark.parametrize("code", [errno.EXDEV, errno.EPERM, errno.ENOTSUP])
def test_a_file_system_without_hard_links_falls_back_to_a_copy(
    home, two, monkeypatch, code
):
    cur, par = two["cur"], two["par"]

    def refuse(src, dst, *args, **kwargs):
        raise OSError(code, "no hard link here")

    monkeypatch.setattr(os, "link", refuse)
    summary = _apply(two, [_act(cur, "c.md", "move", to_dir=par)])
    monkeypatch.undo()
    assert summary["ok"] is True
    assert not (cur / "c.md").exists()
    assert (par / "c.md").read_text(encoding="utf-8") == (
        "---\ndescription: desc c\n---\nbody c\n"
    )
    assert not [p for p in par.iterdir() if TEMP_NAME.match(p.name)]


def test_identical_source_and_destination_are_a_move_in_progress(home, two):
    cur, par = two["cur"], two["par"]
    (par / "c.md").write_bytes((cur / "c.md").read_bytes())
    summary = _apply(two, [_act(cur, "c.md", "move", to_dir=par)])
    assert summary["ok"] is True
    assert summary["results"][0]["status"] == "done"
    assert not (cur / "c.md").exists()
    assert (par / "c.md").read_text(encoding="utf-8") == (
        "---\ndescription: desc c\n---\nbody c\n"
    )
    assert (par / "MEMORY.md").read_text(encoding="utf-8").count("(c.md)") == 1
    assert "c.md" not in (cur / "MEMORY.md").read_text(encoding="utf-8")
    assert "c.md" not in _json(cur / LEDGER_NAME)["notes"]


def test_different_source_and_destination_are_refused_naming_both_paths(home, two):
    cur, par = two["cur"], two["par"]
    _note(par, "c.md", "another body\n")
    with pytest.raises(ap.PlanError) as refused:
        ap.parse_plan({"actions": [_act(cur, "c.md", "move", to_dir=par)]}, two["repo"])
    assert _short(cur / "c.md", home) in str(refused.value)
    assert _short(par / "c.md", home) in str(refused.value)


def test_a_destination_that_differs_when_applying_is_refused_and_nothing_is_lost(
    home, two
):
    cur, par = two["cur"], two["par"]
    plan = ap.parse_plan(
        {"actions": [_act(cur, "c.md", "move", to_dir=par)]}, two["repo"]
    )
    (par / "c.md").write_text("theirs\n", encoding="utf-8")
    summary = ap.apply_plan(plan, TODAY)
    assert summary["ok"] is False
    assert (par / "c.md").read_text(encoding="utf-8") == "theirs\n"
    assert (cur / "c.md").exists()
    assert "c.md" in (cur / "MEMORY.md").read_text(encoding="utf-8")


def test_hidden_temporaries_of_a_dead_run_are_cleaned_and_real_files_kept(home, two):
    cur, par = two["cur"], two["par"]
    junk = [
        ".pc-memory-move.abcd1234",
        ".pc-memory-index.zzzz0000",
        ".pc-memory-ledger.12345678",
    ]
    keep = [LOCK_NAME, LEDGER_NAME + ".corrupt", ".pc-memory-ledger.abc", "notes.txt"]
    for directory in (cur, par):
        for name in junk + keep:
            (directory / name).write_text("x", encoding="utf-8")
    _apply(two, [_act(cur, "a.md", "keep")], reviewed=[par])
    for directory in (cur, par):
        assert not [n for n in junk if (directory / n).exists()]
        assert all((directory / n).exists() for n in keep)


# --- review round 3: the approval covers what runs ----------------------------------


def test_the_skill_asks_for_the_plan_shown_with_a_separate_permanent_deletion_option():
    text = (PLUGIN / "skills" / "reconcile" / "SKILL.md").read_text(encoding="utf-8")
    assert "exactly the plan shown" in text
    assert "never the default" in text


# --- review round 3: raw fields -----------------------------------------------------


def test_every_raw_inventory_field_is_cleaned_and_capped(home, tree):
    cur = tree["current"]
    bad = "ev\U000e0041il\x1b[0m‮"
    (cur / f"{bad}.md").write_text("Same body across levels.\n", encoding="utf-8")
    (cur / "MEMORY.md").write_text(
        "- [g](ghost\U000e0041‮.md)\n- [t](build_trap.md)\n", encoding="utf-8"
    )
    ledger = _json(cur / LEDGER_NAME)
    ledger["notes"]["build_trap.md"]["last_read"] = "\x1b[31m2026-10-01‮"
    ledger["notes"]["tone.md"] = {"sessions": 1, "last_read": "x" * 3000}
    ledger["last_review"] = "‮2026-08-01" + "z" * 3000
    (cur / LEDGER_NAME).write_text(json.dumps(ledger), encoding="utf-8")
    level = inv.build_inventory(tree["repo"], NOW)["levels"][0]
    by_file = {n["file"]: n for n in level["notes"]}
    assert "evil[0m.md" in by_file
    assert level["orphans"]["file_without_index"] == [
        "dup.md",
        "evil[0m.md",
        "loose.md",
        "tone.md",
    ]
    assert level["orphans"]["index_without_file"] == ["ghost.md"]
    assert by_file["build_trap.md"]["last_read"] is None
    assert by_file["tone.md"]["last_read"] is None
    assert level["last_review"] is None
    exact = inv.build_inventory(tree["repo"], NOW)["duplicates"]["exact"]
    assert [m["file"] for g in exact for m in g] == [
        "dup_shared.md",
        "dup.md",
        "evil[0m.md",
    ]


def test_a_description_cannot_make_a_second_link_in_a_generated_index_line(tmp_path):
    note = tmp_path / "n.md"
    note.write_text(
        "---\ndescription: see [z](z.md) \x1b[31m‮\U000e0041 "
        + "d" * 3000
        + "\n---\nb\n",
        encoding="utf-8",
    )
    line = ap._default_line(note, "n.md")
    from pc_memory_lib import link_targets

    assert link_targets(line) == ["n.md"]
    assert len(line) <= 260
    assert "\x1b" not in line and "‮" not in line and "\U000e0041" not in line


def test_a_note_name_with_brackets_makes_one_link(tmp_path):
    from pc_memory_lib import link_targets

    note = tmp_path / "a](evil.md).md"
    note.write_text("b\n", encoding="utf-8")
    targets = link_targets(ap._default_line(note, note.name))
    assert note.name in targets
    assert set(targets) <= {note.name, quote(note.name)}


# --- review round 3: the index is a real file ---------------------------------------


def test_an_index_that_is_a_symlink_is_refused_when_the_plan_is_read(
    home, two, tmp_path
):
    real = tmp_path / "real-index.md"
    real.write_text("- [P](p.md)\n", encoding="utf-8")
    (two["par"] / "MEMORY.md").unlink()
    (two["par"] / "MEMORY.md").symlink_to(real)
    with pytest.raises(ap.PlanError, match="symlink"):
        ap.parse_plan({"actions": [_act(two["par"], "p.md", "delete")]}, two["repo"])
    assert real.read_text(encoding="utf-8") == "- [P](p.md)\n"


def test_an_index_replaced_by_a_symlink_after_validation_is_not_overwritten(
    home, two, tmp_path
):
    real = tmp_path / "real-index.md"
    real.write_text("- [A](a.md)\n", encoding="utf-8")
    plan = ap.parse_plan({"actions": [_act(two["cur"], "a.md", "delete")]}, two["repo"])
    (two["cur"] / "MEMORY.md").unlink()
    (two["cur"] / "MEMORY.md").symlink_to(real)
    summary = ap.apply_plan(plan, TODAY)
    assert summary["ok"] is False
    assert (two["cur"] / "MEMORY.md").is_symlink()
    assert real.read_text(encoding="utf-8") == "- [A](a.md)\n"


# --- review round 3: the plan text --------------------------------------------------


def _cli(home, two, text, *extra):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "apply.py"),
            "--plan",
            "-",
            "--cwd",
            str(two["repo"]),
            *extra,
        ],
        input=text,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
        check=False,
    )


def test_a_plan_with_a_duplicated_key_is_refused(home, two):
    cur = two["cur"]
    text = (
        '{"actions": [{"dir": "%s", "note": "a.md", "action": "keep", "action": "delete"}]}'
        % cur
    )
    before = _snapshot(home / ".claude")
    run = _cli(home, two, text)
    assert run.returncode == 2
    assert "duplicate" in json.loads(run.stdout)["refused"]
    assert _snapshot(home / ".claude") == before
    assert (cur / "a.md").exists()


def test_a_plan_nested_too_deep_is_refused_and_nothing_is_modified(home, two):
    before = _snapshot(home / ".claude")
    run = _cli(home, two, "[" * 200000)
    assert run.returncode == 2
    assert json.loads(run.stdout)["ok"] is False
    assert _snapshot(home / ".claude") == before


def test_thousands_of_actions_do_not_slow_the_run_down(home, two, monkeypatch):
    cur = two["cur"]
    calls = []
    real = ap.Plan.removable_by_dir

    def counted(self):
        calls.append(1)
        return real(self)

    monkeypatch.setattr(ap.Plan, "removable_by_dir", counted)
    start = time.monotonic()
    summary = _apply(
        two,
        [_act(cur, "a.md", "keep")] * 1
        + [_act(cur, f"k{n}.md", "keep") for n in range(8000)],
    )
    assert summary["ok"] is True
    assert len(calls) == 1
    assert time.monotonic() - start < 5


def test_the_refusal_for_a_fresh_marker_says_what_to_do_after_a_kill(home, two):
    (two["cur"] / MARKER_NAME).write_text("", encoding="utf-8")
    with pytest.raises(ap.PlanError, match="killed.*10 minutes.*remove"):
        ap.parse_plan({"actions": [_act(two["cur"], "a.md", "keep")]}, two["repo"])


# --- review round 3: the counter ----------------------------------------------------


def test_a_count_started_before_a_deletion_does_not_recreate_the_pruned_entry(
    home, two
):
    import fcntl
    import threading

    cur = two["cur"]
    ledger_before = _json(cur / LEDGER_NAME)
    assert "a.md" in ledger_before["notes"]
    result = []
    with open(cur / LOCK_NAME, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        thread = threading.Thread(
            target=lambda: result.append(
                cnt.record_read(_read_payload(two, "s9"), TODAY)
            )
        )
        thread.start()
        time.sleep(0.2)
        (cur / "a.md").unlink()
        ledger = _json(cur / LEDGER_NAME)
        del ledger["notes"]["a.md"]
        (cur / LEDGER_NAME).write_text(json.dumps(ledger), encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    thread.join(timeout=5)
    assert result == [False]
    assert "a.md" not in _json(cur / LEDGER_NAME)["notes"]


# --- coverage hints -----------------------------------------------------------------


@pytest.fixture
def one(home):
    """One level `home/repo` with an empty memory directory, returns (repo, memory dir)."""
    repo = home / "repo"
    (repo / ".git").mkdir(parents=True)
    memory = _silo(home, repo)
    (memory / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
    return repo, memory


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


FACT = "Automode configuration settings location overrides"


def _coverage(repo: Path, file: str = "automode_settings_location.md") -> list[dict]:
    notes = inv.build_inventory(repo, NOW)["levels"][0]["notes"]
    return next(n for n in notes if n["file"] == file)["coverage"]


def _automode_note(memory: Path, description: str | None = None) -> None:
    _note(
        memory,
        "automode_settings_location.md",
        "body\n",
        description=description
        or "Automode configuration lives in the user settings, overrides never apply",
    )


def test_a_note_stated_in_a_rule_file_gets_that_file_and_line(home, one):
    repo, memory = one
    _automode_note(memory)
    _write(
        repo / ".claude" / "rules" / "modes.md",
        "# Modes\n\nunrelated line\nAutomode configuration and settings location, no overrides.\n",
    )
    assert _coverage(repo) == [
        {
            "file": _short(repo / ".claude" / "rules" / "modes.md", home),
            "line": 4,
            "text": "Automode configuration and settings location, no overrides.",
            "terms": ["automode", "configuration", "location", "overrides", "settings"],
        }
    ]


def test_a_note_with_no_match_has_an_empty_list(home, one):
    repo, memory = one
    _automode_note(memory)
    _write(repo / "CLAUDE.md", "Nothing about modes here.\nTotally unrelated text.\n")
    assert _coverage(repo) == []


def test_a_single_weak_term_is_not_coverage(home, one):
    repo, memory = one
    _automode_note(memory)
    _write(repo / "CLAUDE.md", "The automode is mentioned once.\n")
    assert _coverage(repo) == []


def test_terms_count_only_within_three_lines(home, one):
    repo, memory = one
    _automode_note(memory)
    far = "automode configuration\n" + "filler\n" * 2 + "settings location overrides\n"
    _write(repo / "CLAUDE.md", far)
    assert _coverage(repo) == []
    near = "automode configuration\n" + "filler\n" + "settings location overrides\n"
    _write(repo / "CLAUDE.md", near)
    assert [m["line"] for m in _coverage(repo)] == [1]


def test_three_rare_terms_are_not_enough(home, one):
    repo, memory = one
    _automode_note(memory)
    _write(repo / "CLAUDE.md", "automode configuration settings\n")
    assert _coverage(repo) == []


def test_one_backticked_identifier_of_eight_characters_is_enough(home, one):
    repo, memory = one
    _automode_note(memory, "Set `autoMemoryDirectory` per level")
    _write(repo / "AGENTS.md", "Use autoMemoryDirectory to redirect.\n")
    assert [(m["file"], m["line"], m["terms"]) for m in _coverage(repo)] == [
        (_short(repo / "AGENTS.md", home), 1, ["automemorydirectory"])
    ]
    _automode_note(memory, "Set `short` per level")
    assert _coverage(repo) == []


def test_a_backticked_identifier_found_everywhere_is_common_and_not_enough(home, one):
    repo, memory = one
    _automode_note(memory, "Set `autoMemoryDirectory` per level")
    _write(repo / "AGENTS.md", "autoMemoryDirectory\n" * 5)
    assert _coverage(repo) == []


def test_common_words_of_the_domain_weigh_nothing(home, one):
    repo, memory = one
    words = (
        "plugin plugins skills scripts working tree files repo claude session memory"
    )
    _note(memory, "tidy.md", "b", description=f"All about {words}")
    _write(repo / "CLAUDE.md", f"{words}\n")
    assert _coverage(repo, "tidy.md") == []


def test_terms_found_in_many_lines_weigh_almost_nothing(home, one):
    repo, memory = one
    _automode_note(memory)
    _write(repo / "CLAUDE.md", (FACT + "\n") * 5)
    assert _coverage(repo) == []


def test_stopwords_are_not_terms(home, one):
    repo, memory = one
    _note(
        memory,
        "tidy.md",
        "b",
        description="Should always be checked before every session",
    )
    _write(repo / "CLAUDE.md", "Should always be checked before every session.\n")
    assert _coverage(repo, "tidy.md") == []


def test_every_kind_of_source_is_read(home, one):
    repo, memory = one
    _automode_note(memory)
    text = FACT + "\n"
    agents = _write(repo / "AGENTS.md", text)
    skill = _write(repo / "plugins" / "p" / "skills" / "s" / "SKILL.md", text)
    _write(repo / "plugins" / "p" / "other" / "SKILL.md", text)
    hooks = _write(
        repo / "plugins" / "p" / "hooks" / "hooks.json",
        '{\n  "description": "' + FACT + '",\n  "command": "' + FACT.lower() + '"\n}\n',
    )
    settings = _write(
        repo / ".claude" / "settings.pinky.json",
        '{\n  "automode": "' + FACT + ' of the user",\n  "other": 1\n}\n',
    )
    for n in range(46):  # keeps each term in few enough files to stay rare
        _write(repo / "skills" / f"f{n:02d}" / "SKILL.md", "Nothing to see here\n")
    filler = {
        _short(repo / "skills" / f"f{n:02d}" / "SKILL.md", home) for n in range(46)
    }
    assert {_short(src.path, home) for src in inv.collect_sources([repo])} == filler | {
        _short(path, home) for path in (agents, skill, hooks, settings)
    }
    got = {(m["file"], m["line"]) for m in _coverage(repo)}
    assert len(got) == 3
    assert got <= {
        (_short(agents, home), 1),
        (_short(skill, home), 1),
        (_short(hooks, home), 2),
        (_short(settings, home), 2),
    }


def test_json_sources_contribute_their_values_only(home, one):
    repo, memory = one
    _automode_note(memory)
    _write(
        repo / ".claude" / "settings.pinky.json",
        '{\n  "automode": 1,\n  "settingsLocation": 2\n}\n',
    )
    _write(
        repo / "hooks" / "hooks.json",
        '{\n  "automode": 1,\n  "command": "' + FACT + '"\n}\n',
    )
    assert _coverage(repo) == []


def test_string_items_of_an_array_in_the_settings_are_read(home, one):
    repo, memory = one
    _automode_note(memory)
    _write(
        repo / ".claude" / "settings.pinky.json",
        '{\n  "gotchas": [\n    "' + FACT + ' is the user file",\n'
        '    "unrelated"\n  ],\n  "count": 3\n}\n',
    )
    assert [(m["file"], m["line"]) for m in _coverage(repo)] == [
        (_short(repo / ".claude" / "settings.pinky.json", home), 3)
    ]


def test_at_most_three_matches_best_first(home, one):
    repo, memory = one
    _automode_note(memory)
    _write(repo / "AGENTS.md", "Automode configuration settings location\n")
    _write(repo / "CLAUDE.md", FACT + "\n")
    for name in ("a", "b"):
        _write(
            repo / ".claude" / "rules" / f"{name}.md",
            "Automode configuration settings location\n",
        )
    for n in range(46):  # keeps each term in few enough files to stay rare
        _write(repo / "skills" / f"s{n:02d}" / "SKILL.md", "Nothing to see here\n")
    got = _coverage(repo)
    assert len(got) == 3
    assert got[0]["file"] == _short(repo / "CLAUDE.md", home)
    assert [m["file"] for m in got[1:]] == [
        _short(repo / "AGENTS.md", home),
        _short(repo / ".claude" / "rules" / "a.md", home),
    ]


def test_an_oversized_file_and_a_symlink_are_ignored(home, one, tmp_path):
    repo, memory = one
    _automode_note(memory)
    big = FACT + "\n" + "x" * 200 * 1024
    _write(repo / "CLAUDE.md", big)
    outside = _write(tmp_path / "outside" / "AGENTS.md", FACT + "\n")
    (repo / "AGENTS.md").symlink_to(outside)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "rules").symlink_to(outside.parent)
    (repo / "skills").symlink_to(outside.parent)
    assert _coverage(repo) == []
    assert inv.collect_sources([repo]) == []
    _write(repo / "CLAUDE.md", FACT + "\n" + "x" * 150 * 1024)
    assert [m["line"] for m in _coverage(repo)] == [1]


def _agents_only(home: Path, repo: Path) -> dict:
    return {
        "file": _short(repo / "AGENTS.md", home),
        "line": 1,
        "text": FACT,
        "terms": ["automode", "configuration", "location", "overrides", "settings"],
    }


def test_claude_md_linked_to_agents_md_is_cited_once_as_agents_md(home, one):
    repo, memory = one
    _automode_note(memory)
    _write(repo / "AGENTS.md", FACT + "\n")
    (repo / "CLAUDE.md").symlink_to("AGENTS.md")
    assert _coverage(repo) == [_agents_only(home, repo)]


def test_agents_md_linked_to_claude_md_is_cited_once_as_agents_md(home, one):
    repo, memory = one
    _automode_note(memory)
    _write(repo / "CLAUDE.md", FACT + "\n")
    (repo / "AGENTS.md").symlink_to("CLAUDE.md")
    assert _coverage(repo) == [_agents_only(home, repo)]


def test_both_names_linked_to_a_third_file_of_the_level_are_cited_once(home, one):
    repo, memory = one
    _automode_note(memory)
    _write(repo / "docs" / "shared.md", FACT + "\n")
    (repo / "AGENTS.md").symlink_to("docs/shared.md")
    (repo / "CLAUDE.md").symlink_to("docs/shared.md")
    assert _coverage(repo) == [_agents_only(home, repo)]


def test_a_link_to_a_file_outside_the_level_or_a_broken_link_is_ignored(
    home, one, tmp_path
):
    repo, memory = one
    _automode_note(memory)
    outside = _write(tmp_path / "elsewhere" / "AGENTS.md", FACT + "\n")
    (repo / "AGENTS.md").symlink_to(outside)
    (repo / "CLAUDE.md").symlink_to("missing.md")
    assert _coverage(repo) == []
    assert inv.collect_sources([repo]) == []


def test_a_rule_linked_to_a_file_of_the_level_is_read_once(home, one):
    repo, memory = one
    _automode_note(memory)
    shared = _write(repo / "notes" / "shared.md", FACT + "\n")
    rules = repo / ".claude" / "rules"
    rules.mkdir(parents=True)
    (rules / "x.md").symlink_to(shared)
    (rules / "y.md").symlink_to(shared)
    assert [m["file"] for m in _coverage(repo)] == [_short(rules / "x.md", home)]


def test_a_skill_linked_outside_the_level_is_ignored_and_one_inside_is_read(
    home, one, tmp_path
):
    repo, memory = one
    _automode_note(memory)
    outside = _write(tmp_path / "elsewhere" / "SKILL.md", FACT + "\n")
    inside = _write(repo / "docs" / "skill.md", FACT + "\n")
    (repo / "skills" / "a").mkdir(parents=True)
    (repo / "skills" / "a" / "SKILL.md").symlink_to(outside)
    (repo / "skills" / "b").mkdir()
    (repo / "skills" / "b" / "SKILL.md").symlink_to(inside)
    assert [m["file"] for m in _coverage(repo)] == [
        _short(repo / "skills" / "b" / "SKILL.md", home)
    ]


def test_at_most_sixty_source_files_are_read_per_level(home, one):
    repo, memory = one
    _automode_note(memory)
    for n in range(70):
        _write(repo / "skills" / f"s{n:02d}" / "SKILL.md", "Automode settings\n")
    assert len(inv.collect_sources([repo])) == 60


def test_a_walk_stops_below_six_directories(home, one):
    repo, memory = one
    _automode_note(memory)
    deep = repo / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "skills" / "s" / "SKILL.md"
    _write(deep, "Automode settings\n")
    assert inv.collect_sources([repo]) == []


def _long_token() -> str:
    """A 28-character token of letters and digits, built at run time."""
    return "".join(f"{chr(97 + i)}{i % 10}" for i in range(14))


SECRET_SHAPES = {
    "known-prefix": lambda: "tok" + "en" + ": " + "gh" + "p_" + "abcdefghij" * 4,
    "keyword-equals": lambda: "api" + "_key" + " = " + "abcdef0123" * 3,
    "key-colon-long-token": lambda: "key" + ": " + _long_token(),
    "long-blob": lambda: "".join(
        chr(65 + i % 26) + chr(97 + i % 26) + str(i % 10) for i in range(14)
    ),
    "pem-header": lambda: "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
    "url-credentials": lambda: "https://" + "user" + ":" + "pw" + "@" + "host.example",
    "jwt": lambda: "ey" + "J" + "abcdefgh" + "." + "abcdefgh" + "." + "sig",
}


@pytest.mark.parametrize("shape", SECRET_SHAPES)
def test_lines_shaped_like_a_secret_are_never_a_hint(home, one, shape):
    repo, memory = one
    _automode_note(memory)
    _write(repo / "CLAUDE.md", f"{FACT.lower()} {SECRET_SHAPES[shape]()}\n")
    assert _coverage(repo) == []
    _write(repo / "CLAUDE.md", f"{FACT.lower()}: see the user file\n")
    assert [m["line"] for m in _coverage(repo)] == [1]


def test_hint_text_is_cleaned_clipped_and_stable(home, one):
    repo, memory = one
    _automode_note(memory)
    long = FACT + " \x1b[31mred‮ " + "y" * 400
    _write(repo / "CLAUDE.md", long + "\n")
    first = _coverage(repo)
    assert first == _coverage(repo)
    assert len(first[0]["text"]) == 160
    assert first[0]["text"].startswith(FACT + " [31mred ")
    assert "\x1b" not in first[0]["text"]


def test_coverage_never_writes(home, one):
    repo, memory = one
    _automode_note(memory)
    _write(repo / "CLAUDE.md", FACT + "\n")
    before = _snapshot(home)
    assert _coverage(repo)
    assert _snapshot(home) == before


def test_a_note_is_checked_against_the_sources_of_every_level_shown(home):
    ws = home / "ws"
    inner = ws / "repo2"
    (inner / ".git").mkdir(parents=True)
    for level in (ws, inner):
        (_silo(home, level) / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
    _write(ws / "CLAUDE.md", FACT + "\n")
    _note(
        (home / ".claude" / "projects" / encode(inner) / "memory"),
        "automode_settings_location.md",
        "b",
        description="Automode configuration lives in settings, overrides never apply",
    )
    notes = inv.build_inventory(inner, NOW)["levels"][0]["notes"]
    assert [m["file"] for m in notes[0]["coverage"]] == [_short(ws / "CLAUDE.md", home)]


FALSE_POSITIVES = [
    pytest.param(
        "claude-code-lsp-recommendation.md",
        "Claude Code's built-in LSP recommendation keeps proposing pyright-lsp@claude-plugins-official "
        "(no working LSP config) even with the pyright@claude-code-lsps fork active \u2014 reinstalled "
        "once on 2026-09-20",
        "AGENTS.md",
        "This repo is registered as a `directory` marketplace. `${CLAUDE_PLUGIN_ROOT}` then resolves to the\n"
        "working tree (`plugins/<name>/`), not to the copy under\n"
        "`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` \u2014 measured on Claude Code 2.1.283\n",
        id="lsp",
    ),
    pytest.param(
        "claude-plugin-eval-cli-gotchas.md",
        "Operational gotchas of `claude plugin eval` discovered while authoring pc-git's commit-and-pr "
        "suite \u2014 tool grants, judge model default, sandbox network/git limits",
        "AGENTS.md",
        "  quality gates (mypy, bandit, interrogate, darglint) run against every authored `.py` under a\n"
        "  plugin \u2014 `hooks/` (concern folders nested inside it included) and `skills/<name>/` alike.\n"
        "- A profile-resolved value (chat language, default branch) is never hardcoded into a public\n",
        id="eval-gotchas",
    ),
    pytest.param(
        "feedback_commit-and-pr-broad-add-sweeps-untracked.md",
        "After running pc-git commit-and-pr (or bulk_commit.sh), check the PR diff for unrelated "
        "pre-existing untracked files it may have swept in",
        "plugins/pc-core/skills/review-guidelines/SKILL.md",
        "\n- Code formatting, import order, line length \u2014 handled by pre-commit (the repo's own formatter)\n"
        "- Linting violations already caught by the repo's own linter\n",
        id="commit-and-pr",
    ),
    pytest.param(
        "feedback_finishing-card-check-selected-for-dev-first.md",
        "When finishing a kanban card and looking for what's next, check 2-selected-for-dev before "
        "proposing from 1-todo",
        "plugins/pc-kanban/skills/kanban/SKILL.md",
        "- **`priority`** \u2014 1 (low) to 5 (urgent). The agent proposes a value at the same moment it writes a\n"
        "  card (`kanban-proactivity.md` \u00a7 Capture \u2192 card) with its reason; the human validates or changes\n"
        "  it before the card is written \u2014 never agent-set unconfirmed. Drives the ordering of the next-card\n"
        "  proposal in `0-ideas`/`1-todo` (`finishing-a-card.md` \u00a7 Proposing the next card) and of the\n"
        '  "highest `priority:`" pick in `2-selected-for-dev` above \u2014 `date:`/file position only tie-break\n'
        "  at equal `priority`. Omitted = `3`, no backfill of existing cards, no error.\n",
        id="finishing-card",
    ),
    pytest.param(
        "feedback_format-only-touched-files.md",
        "Run ruff format on the files you edited, never on `plugins` or the whole repo",
        "AGENTS.md",
        "\n- **Plugin** \u2014 a folder under `plugins/<name>/` with its own `.claude-plugin/plugin.json`,\n"
        "  published through the root `.claude-plugin/marketplace.json`.\n",
        id="format-only",
    ),
]


@pytest.mark.parametrize(("note", "description", "source", "text"), FALSE_POSITIVES)
def test_lines_that_share_only_the_vocabulary_of_the_repo_are_not_coverage(
    home, one, note, description, source, text
):
    repo, memory = one
    _note(memory, note, "body\n", description=description)
    _write(repo / source, text)
    assert _coverage(repo, note) == []


def test_a_rule_stating_the_same_fact_in_other_words_is_coverage_with_its_terms(
    home, one
):
    repo, memory = one
    _note(
        memory,
        "format_touched_paths.md",
        "body\n",
        description="Run the formatter on the files you edited, never on the whole repository",
    )
    rule = _write(
        repo / ".claude" / "rules" / "formatting.md",
        "# Formatting\n"
        "Run the formatter per path: format only what you edited, touched paths only, "
        "never the repository as a whole.\n",
    )
    assert _coverage(repo, "format_touched_paths.md") == [
        {
            "file": _short(rule, home),
            "line": 2,
            "text": "Run the formatter per path: format only what you edited, touched paths only, "
            "never the repository as a whole.",
            "terms": ["edited", "format", "formatter", "repository", "touched"],
        }
    ]


# --- the skill asks the model to use the hints --------------------------------------


def _skill_text() -> str:
    """The skill with its line wraps and list indentation collapsed to single spaces."""
    text = (PLUGIN / "skills" / "reconcile" / "SKILL.md").read_text(encoding="utf-8")
    return " ".join(text.split())


def test_the_skill_reads_the_coverage_hints_and_recommends_deleting_a_covered_note():
    text = _skill_text()
    assert "`coverage`" in text
    assert "recommend delete" in text.lower()
    assert "path and line" in text
    assert "a clue, not a finding" in text
    assert "`terms`" in text
    assert "only after reading the cited line" in text
    assert "states the same fact as the note" in text
    assert "never recommend keeping a note that a source already covers" in text
    assert "never recommend keep because of a vague hint" in text


def test_the_skill_groups_the_covered_notes_in_one_first_question():
    text = _skill_text()
    assert "two or more notes are covered" in text
    assert "ONE grouped question" in text
    assert "delete all N covered notes (recommended)" in text
    assert "go through them one by one" in text
    assert "keep them all" in text
    assert "<short title> — <summary> — covered by <source path:line>" in text


def test_the_skill_asks_each_note_with_its_title_path_and_summary():
    text = _skill_text()
    assert "the note title, never a bare file name" in text
    assert "full clickable `~/…` path on its own line" in text
    assert "1–2 line summary" in text
    assert "the covering source" in text


def test_the_skill_gives_every_question_a_topic_header_not_a_file_stem_or_number():
    text = _skill_text()
    assert "`header`" in text
    assert "1–2 word topic" in text
    assert "not the file name stem, not a number" in text


def test_the_skill_opens_each_batch_with_one_intro_line_that_is_never_cut():
    text = _skill_text()
    assert "Batch 2/4 — 3 notes" in text
    assert "never cut" in text


def test_the_skill_asks_only_where_the_choice_is_real_and_has_one_worked_example():
    text = _skill_text()
    assert "only where the choice is real" in text
    assert "Obvious cases" in text
    assert text.count("Worked example") == 1
