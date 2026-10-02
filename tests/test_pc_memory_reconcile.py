"""Tests for the pc-memory `reconcile` scripts — run with: uv run pytest tests/test_pc_memory_reconcile.py"""

import json
import os
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
    with pytest.raises(ap.PlanError, match="already exists"):
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
            _act(cur, "ghost.md", "delete"),
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
            {"dir": short_cur, "note": "a.md", "action": "delete", "status": "planned"},
            {"dir": short_cur, "note": "c.md", "action": "move", "status": "planned"},
            {
                "dir": short_cur,
                "note": "ghost.md",
                "action": "delete",
                "status": "already_gone",
            },
        ],
        "last_review": {short_cur: "2026-10-02"},
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
    assert "--plan -" in text


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
        ap._move_exclusive(source, dest)
    assert dest.read_text(encoding="utf-8") == "theirs\n"
    assert source.read_text(encoding="utf-8") == "mine\n"


def test_a_copy_that_fails_midway_leaves_no_half_file(tmp_path, monkeypatch):
    source, dest = tmp_path / "s.md", tmp_path / "d.md"
    source.write_text("mine\n", encoding="utf-8")

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ap.shutil, "copyfileobj", broken)
    with pytest.raises(OSError, match="disk full"):
        ap._move_exclusive(source, dest)
    monkeypatch.undo()
    assert source.exists() and not dest.exists()
