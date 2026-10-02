"""Tests for the pc-memory PostToolUse consultation counter — run with: uv run pytest tests/test_pc_memory_count.py"""

import json
import fcntl
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "pc-memory"
HOOK = PLUGIN / "hooks" / "memory-count" / "memory_count.py"
sys.path.insert(0, str(HOOK.parent))
sys.path.insert(0, str(PLUGIN / "hooks" / "memory-context"))

import memory_context as mc  # noqa: E402
import memory_count as cnt  # noqa: E402
from pc_memory_lib import (  # noqa: E402
    LEDGER_NAME,
    LOCK_NAME,
    MARKER_NAME,
    encode,
)

DAY1 = date(2026, 10, 2)
DAY2 = date(2026, 10, 3)


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


@pytest.fixture
def repo(home):
    """home/ws/repo, a git root."""
    path = home / "ws" / "repo"
    (path / ".git").mkdir(parents=True)
    return path


@pytest.fixture
def silo(home, repo):
    """The default memory silo of `repo`, with an index and one note."""
    directory = home / ".claude" / "projects" / encode(repo) / "memory"
    directory.mkdir(parents=True)
    (directory / "MEMORY.md").write_text("- [a](a.md)\n", encoding="utf-8")
    (directory / "a.md").write_text("body of a\n", encoding="utf-8")
    return directory


def _read(path: Path | str, cwd: Path, session: str, tool: str = "Read") -> dict:
    return {
        "tool_name": tool,
        "session_id": session,
        "cwd": str(cwd),
        "tool_input": {"file_path": str(path)},
    }


def _ledger(directory: Path) -> dict:
    return json.loads((directory / LEDGER_NAME).read_text(encoding="utf-8"))


def _run(payload: str, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
        timeout=15,
    )


# --- counting: once per session -----------------------------------------------------------


def test_two_reads_in_one_session_count_once(home, repo, silo):
    payload = _read(silo / "a.md", repo, "s1")
    assert cnt.record_read(payload, DAY1) is True
    assert cnt.record_read(payload, DAY1) is True
    assert _ledger(silo) == {
        "notes": {
            "a.md": {"sessions": 1, "last_session": "s1", "last_read": "2026-10-02"}
        },
        "last_review": None,
    }


def test_same_note_in_two_sessions_counts_twice(home, repo, silo):
    cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1)
    cnt.record_read(_read(silo / "a.md", repo, "s2"), DAY2)
    assert _ledger(silo) == {
        "notes": {
            "a.md": {"sessions": 2, "last_session": "s2", "last_read": "2026-10-03"}
        },
        "last_review": None,
    }


def test_last_read_moves_with_the_day_inside_one_session(home, repo, silo):
    cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1)
    cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY2)
    assert _ledger(silo)["notes"]["a.md"] == {
        "sessions": 1,
        "last_session": "s1",
        "last_read": "2026-10-03",
    }


def test_each_note_has_its_own_counter(home, repo, silo):
    (silo / "b.md").write_text("body of b\n", encoding="utf-8")
    cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1)
    cnt.record_read(_read(silo / "b.md", repo, "s1"), DAY1)
    cnt.record_read(_read(silo / "a.md", repo, "s2"), DAY1)
    assert _ledger(silo)["notes"] == {
        "a.md": {"sessions": 2, "last_session": "s2", "last_read": "2026-10-02"},
        "b.md": {"sessions": 1, "last_session": "s1", "last_read": "2026-10-02"},
    }


def test_default_date_is_today_local(home, repo, silo):
    cnt.record_read(_read(silo / "a.md", repo, "s1"))
    assert _ledger(silo)["notes"]["a.md"]["last_read"] == date.today().isoformat()


def test_relative_path_resolves_from_cwd(home, repo, silo):
    cnt.record_read(_read("a.md", silo, "s1"), DAY1)
    assert list(_ledger(silo)["notes"]) == ["a.md"]


def test_a_note_of_another_project_silo_counts(home, repo, silo):
    other = home / ".claude" / "projects" / "-some-other-project" / "memory"
    other.mkdir(parents=True)
    (other / "n.md").write_text("x\n", encoding="utf-8")
    cnt.record_read(_read(other / "n.md", repo, "s1"), DAY1)
    assert _ledger(other) == {
        "notes": {
            "n.md": {"sessions": 1, "last_session": "s1", "last_read": "2026-10-02"}
        },
        "last_review": None,
    }


def test_a_directory_declared_in_local_settings_counts(home, repo):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text(
        json.dumps({"autoMemoryDirectory": "notes/mem"}), encoding="utf-8"
    )
    declared = repo / "notes" / "mem"
    declared.mkdir(parents=True)
    (declared / "d.md").write_text("x\n", encoding="utf-8")
    cnt.record_read(_read(declared / "d.md", repo, "s1"), DAY1)
    assert _ledger(declared) == {
        "notes": {
            "d.md": {"sessions": 1, "last_session": "s1", "last_read": "2026-10-02"}
        },
        "last_review": None,
    }


def test_a_directory_declared_in_user_settings_counts(home, repo):
    declared = home / "elsewhere" / "mem"
    declared.mkdir(parents=True)
    (declared / "d.md").write_text("x\n", encoding="utf-8")
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": str(declared)}), encoding="utf-8"
    )
    cnt.record_read(_read(declared / "d.md", repo, "s1"), DAY1)
    assert list(_ledger(declared)["notes"]) == ["d.md"]


def test_a_directory_declared_in_shared_project_settings_receives_nothing(home, repo):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": "notes/mem"}), encoding="utf-8"
    )
    declared = repo / "notes" / "mem"
    declared.mkdir(parents=True)
    (declared / "d.md").write_text("x\n", encoding="utf-8")
    assert cnt.record_read(_read(declared / "d.md", repo, "s1"), DAY1) is False
    assert sorted(p.name for p in declared.iterdir()) == ["d.md"]


def test_local_settings_win_over_shared_project_settings(home, repo):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": "shared/mem"}), encoding="utf-8"
    )
    (repo / ".claude" / "settings.local.json").write_text(
        json.dumps({"autoMemoryDirectory": "local/mem"}), encoding="utf-8"
    )
    for name in ("shared", "local"):
        (repo / name / "mem").mkdir(parents=True)
        (repo / name / "mem" / "d.md").write_text("x\n", encoding="utf-8")
    cnt.record_read(_read(repo / "shared" / "mem" / "d.md", repo, "s1"), DAY1)
    cnt.record_read(_read(repo / "local" / "mem" / "d.md", repo, "s1"), DAY1)
    assert sorted(p.name for p in (repo / "shared" / "mem").iterdir()) == ["d.md"]
    assert LEDGER_NAME in {p.name for p in (repo / "local" / "mem").iterdir()}


def test_the_default_silo_still_counts_when_shared_settings_declare_a_directory(
    home, repo, silo
):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": "notes/mem"}), encoding="utf-8"
    )
    assert cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1) is True


def test_a_symlinked_working_directory_still_finds_the_declared_directory(
    home, repo, tmp_path_factory
):
    outside = tmp_path_factory.mktemp("outside").resolve() / "repo"
    (outside / ".git").mkdir(parents=True)
    (outside / ".claude").mkdir()
    (outside / ".claude" / "settings.local.json").write_text(
        json.dumps({"autoMemoryDirectory": "notes/mem"}), encoding="utf-8"
    )
    declared = outside / "notes" / "mem"
    declared.mkdir(parents=True)
    (declared / "d.md").write_text("x\n", encoding="utf-8")
    link = home / "link"
    link.symlink_to(outside, target_is_directory=True)
    cnt.record_read(_read(link / "notes" / "mem" / "d.md", link, "s1"), DAY1)
    assert _ledger(declared) == {
        "notes": {
            "d.md": {"sessions": 1, "last_session": "s1", "last_read": "2026-10-02"}
        },
        "last_review": None,
    }


# --- what is not counted ------------------------------------------------------------------


def test_the_index_is_not_counted(home, repo, silo):
    assert cnt.record_read(_read(silo / "MEMORY.md", repo, "s1"), DAY1) is False
    assert sorted(p.name for p in silo.iterdir()) == ["MEMORY.md", "a.md"]


@pytest.mark.parametrize(
    "where",
    ["repo_note", "sub_folder_note", "silo_text_file", "home_note", "missing_note"],
)
def test_a_file_outside_a_memory_directory_writes_nothing(home, repo, silo, where):
    (repo / "readme.md").write_text("x\n", encoding="utf-8")
    (silo / "sub").mkdir()
    (silo / "sub" / "deep.md").write_text("x\n", encoding="utf-8")
    (silo / "notes.txt").write_text("x\n", encoding="utf-8")
    (home / "top.md").write_text("x\n", encoding="utf-8")
    target = {
        "repo_note": repo / "readme.md",
        "sub_folder_note": silo / "sub" / "deep.md",
        "silo_text_file": silo / "notes.txt",
        "home_note": home / "top.md",
        "missing_note": silo / "gone.md",
    }[where]
    before = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    assert cnt.record_read(_read(target, repo, "s1"), DAY1) is False
    assert sorted(str(p.relative_to(home)) for p in home.rglob("*")) == before


def test_another_tool_is_not_counted(home, repo, silo):
    assert (
        cnt.record_read(_read(silo / "a.md", repo, "s1", tool="Write"), DAY1) is False
    )
    assert not (silo / LEDGER_NAME).exists()


def test_a_payload_without_session_is_not_counted(home, repo, silo):
    payload = _read(silo / "a.md", repo, "s1")
    del payload["session_id"]
    assert cnt.record_read(payload, DAY1) is False
    assert not (silo / LEDGER_NAME).exists()


def test_the_marker_suspends_the_count(home, repo, silo):
    (silo / MARKER_NAME).write_text("", encoding="utf-8")
    assert cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1) is False
    assert not (silo / LEDGER_NAME).exists()


def test_the_marker_leaves_an_existing_ledger_untouched(home, repo, silo):
    cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1)
    before = (silo / LEDGER_NAME).read_bytes()
    (silo / MARKER_NAME).write_text("", encoding="utf-8")
    cnt.record_read(_read(silo / "a.md", repo, "s2"), DAY2)
    assert (silo / LEDGER_NAME).read_bytes() == before


def test_the_marker_of_another_directory_does_not_suspend(home, repo, silo):
    other = home / ".claude" / "projects" / "-other" / "memory"
    other.mkdir(parents=True)
    (other / MARKER_NAME).write_text("", encoding="utf-8")
    assert cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1) is True


# --- the ledger is not a note -------------------------------------------------------------


def test_the_ledger_files_are_not_notes_for_the_startup_message(home, repo, silo):
    (silo / "b.md").write_text("body of b\n", encoding="utf-8")
    assert mc.note_stats(silo, 30, time.time()).total == 2
    cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1)
    (silo / MARKER_NAME).write_text("", encoding="utf-8")
    assert sorted(p.name for p in silo.iterdir()) == sorted(
        [MARKER_NAME, LEDGER_NAME, LOCK_NAME, "MEMORY.md", "a.md", "b.md"]
    )
    stats = mc.note_stats(silo, 30, time.time())
    assert stats.total == 2
    assert stats.stale == []


def test_the_startup_message_is_unchanged_by_the_ledger(home, repo, silo):
    now = time.time()
    levels = mc.collect_levels(repo)
    before = mc.build_user_message(levels, repo, 30, now)
    cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1)
    assert mc.build_user_message(levels, repo, 30, now) == before
    assert before == (
        "\U0001f4da Memory\n"
        "\U0001f4c2 repo \u00b7 1 note \u00b7 last review: - \U0001f4cd\n"
        "\n"
        "\U0001f4a1 Audit your memory: /memory"
    )


# --- the notes are never touched ----------------------------------------------------------


def test_note_and_index_bytes_and_mtime_are_unchanged(home, repo, silo):
    for name in ("a.md", "MEMORY.md"):
        os.utime(silo / name, (1_700_000_000, 1_700_000_000))
    before = {
        name: ((silo / name).read_bytes(), (silo / name).stat().st_mtime_ns)
        for name in ("a.md", "MEMORY.md")
    }
    cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1)
    cnt.record_read(_read(silo / "a.md", repo, "s2"), DAY1)
    cnt.record_read(_read(silo / "MEMORY.md", repo, "s2"), DAY1)
    assert {
        name: ((silo / name).read_bytes(), (silo / name).stat().st_mtime_ns)
        for name in ("a.md", "MEMORY.md")
    } == before


def test_counting_leaves_no_temp_file(home, repo, silo):
    for n in range(5):
        cnt.record_read(_read(silo / "a.md", repo, f"s{n}"), DAY1)
    assert sorted(p.name for p in silo.iterdir()) == sorted(
        [LEDGER_NAME, LOCK_NAME, "MEMORY.md", "a.md"]
    )


# --- rewriting an existing ledger ---------------------------------------------------------


def test_unknown_keys_and_last_review_survive_a_rewrite(home, repo, silo):
    (silo / LEDGER_NAME).write_text(
        json.dumps(
            {
                "notes": {
                    "a.md": {
                        "sessions": 4,
                        "last_session": "old",
                        "last_read": "2026-09-01",
                        "pinned": True,
                    },
                    "z.md": {"sessions": 9, "last_session": "q", "last_read": "x"},
                },
                "last_review": "2026-08-15",
                "schema": 1,
            }
        ),
        encoding="utf-8",
    )
    cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1)
    assert _ledger(silo) == {
        "notes": {
            "a.md": {
                "sessions": 5,
                "last_session": "s1",
                "last_read": "2026-10-02",
                "pinned": True,
            },
            "z.md": {"sessions": 9, "last_session": "q", "last_read": "x"},
        },
        "last_review": "2026-08-15",
        "schema": 1,
    }


def test_a_corrupt_ledger_is_rebuilt_and_the_original_kept(home, repo, silo):
    (silo / LEDGER_NAME).write_text('{"notes": {"a.md": {"sess', encoding="utf-8")
    assert cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1) is True
    assert _ledger(silo) == {
        "notes": {
            "a.md": {"sessions": 1, "last_session": "s1", "last_read": "2026-10-02"}
        },
        "last_review": None,
    }
    corrupt = silo / (LEDGER_NAME + ".corrupt")
    assert corrupt.read_text(encoding="utf-8") == '{"notes": {"a.md": {"sess'


def test_a_new_corruption_overwrites_the_previous_corrupt_copy(home, repo, silo):
    (silo / (LEDGER_NAME + ".corrupt")).write_text("older", encoding="utf-8")
    (silo / LEDGER_NAME).write_text("newer garbage", encoding="utf-8")
    cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1)
    assert (silo / (LEDGER_NAME + ".corrupt")).read_text() == "newer garbage"


def test_a_ledger_that_is_not_utf8_is_rebuilt_and_kept(home, repo, silo):
    (silo / LEDGER_NAME).write_bytes(b'{"notes": {"\xff\xfe": {}}}')
    assert cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1) is True
    assert _ledger(silo) == {
        "notes": {
            "a.md": {"sessions": 1, "last_session": "s1", "last_read": "2026-10-02"}
        },
        "last_review": None,
    }
    assert (silo / (LEDGER_NAME + ".corrupt")).read_bytes() == (
        b'{"notes": {"\xff\xfe": {}}}'
    )


def test_a_valid_ledger_leaves_no_corrupt_copy(home, repo, silo):
    cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1)
    cnt.record_read(_read(silo / "a.md", repo, "s2"), DAY1)
    assert not (silo / (LEDGER_NAME + ".corrupt")).exists()


def test_a_ledger_that_is_a_directory_is_left_alone(home, repo, silo):
    (silo / LEDGER_NAME).mkdir()
    result = _run(json.dumps(_read(silo / "a.md", repo, "s1")), home)
    assert (result.returncode, result.stdout) == (0, "")
    assert result.stderr.startswith("memory_count:")
    assert (silo / LEDGER_NAME).is_dir()
    assert not (silo / (LEDGER_NAME + ".corrupt")).exists()


def test_a_ledger_that_is_not_an_object_is_rebuilt(home, repo, silo):
    (silo / LEDGER_NAME).write_text("[1, 2]", encoding="utf-8")
    cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1)
    assert (silo / (LEDGER_NAME + ".corrupt")).read_text() == "[1, 2]"
    assert _ledger(silo) == {
        "notes": {
            "a.md": {"sessions": 1, "last_session": "s1", "last_read": "2026-10-02"}
        },
        "last_review": None,
    }


def test_a_damaged_ledger_keeps_what_is_usable(home, repo, silo):
    (silo / LEDGER_NAME).write_text(
        json.dumps(
            {
                "notes": {
                    "a.md": {"sessions": "many", "last_session": "old"},
                    "b.md": "oops",
                    "c.md": {"sessions": 3, "last_session": "c1", "last_read": "d"},
                },
                "last_review": "2026-08-15",
            }
        ),
        encoding="utf-8",
    )
    cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1)
    assert _ledger(silo) == {
        "notes": {
            "a.md": {"sessions": 1, "last_session": "s1", "last_read": "2026-10-02"},
            "c.md": {"sessions": 3, "last_session": "c1", "last_read": "d"},
        },
        "last_review": "2026-08-15",
    }


def test_an_unreadable_ledger_is_not_overwritten(home, repo, silo):
    (silo / LEDGER_NAME).write_text('{"notes": {}}', encoding="utf-8")
    (silo / LEDGER_NAME).chmod(0)
    try:
        if os.access(silo / LEDGER_NAME, os.R_OK):
            pytest.skip("permissions are not enforced for this user")
        result = _run(json.dumps(_read(silo / "a.md", repo, "s1")), home)
    finally:
        (silo / LEDGER_NAME).chmod(0o644)
    assert (result.returncode, result.stdout) == (0, "")
    assert result.stderr.startswith("memory_count:")
    assert (silo / LEDGER_NAME).read_text(encoding="utf-8") == '{"notes": {}}'


# --- the process: never fails, never prints -----------------------------------------------


def test_the_process_counts_and_prints_nothing(home, repo, silo):
    result = _run(json.dumps(_read(silo / "a.md", repo, "s1")), home)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert _ledger(silo) == {
        "notes": {
            "a.md": {
                "sessions": 1,
                "last_session": "s1",
                "last_read": date.today().isoformat(),
            }
        },
        "last_review": None,
    }


def test_the_process_survives_a_corrupt_ledger(home, repo, silo):
    (silo / LEDGER_NAME).write_text("not json at all", encoding="utf-8")
    result = _run(json.dumps(_read(silo / "a.md", repo, "s1")), home)
    assert (result.returncode, result.stdout) == (0, "")
    assert result.stderr.startswith("memory_count: corrupt ledger")
    assert _ledger(silo)["notes"]["a.md"]["sessions"] == 1


@pytest.mark.parametrize(
    "stdin",
    ["", "not json", "[]", "null", '{"tool_name": "Read"}', '{"tool_input": 3}'],
)
def test_an_unreadable_payload_exits_zero_with_no_output(home, repo, silo, stdin):
    result = _run(stdin, home)
    assert (result.returncode, result.stdout) == (0, "")
    assert not (silo / LEDGER_NAME).exists()


def test_the_process_leaves_a_non_memory_read_alone(home, repo, silo):
    (repo / "readme.md").write_text("x\n", encoding="utf-8")
    result = _run(json.dumps(_read(repo / "readme.md", repo, "s1")), home)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert sorted(p.name for p in silo.iterdir()) == ["MEMORY.md", "a.md"]


# --- concurrency --------------------------------------------------------------------------

_LOOP = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import memory_count
directory, cwd, prefix = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
for n in range(50):
    memory_count.record_read({
        "tool_name": "Read", "session_id": f"{prefix}-{n}", "cwd": cwd,
        "tool_input": {"file_path": str(directory / "a.md")},
    })
"""


def test_two_processes_incrementing_one_note_lose_nothing(home, repo, silo):
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _LOOP, str(HOOK.parent), str(silo), str(repo), p],
            env={**os.environ, "HOME": str(home)},
        )
        for p in ("p", "q")
    ]
    assert [proc.wait(timeout=120) for proc in procs] == [0, 0]
    entry = _ledger(silo)["notes"]["a.md"]
    assert entry["sessions"] == 100
    assert entry["last_session"] in {"p-49", "q-49"}


def _worker(barrier, directory: str, cwd: str, prefix: str) -> None:
    barrier.wait()
    for n in range(50):
        cnt.record_read(
            _read(Path(directory) / "a.md", Path(cwd), f"{prefix}-{n}"), DAY1
        )


def test_forked_workers_incrementing_one_note_lose_nothing(home, repo, silo):
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(4)
    workers = [
        ctx.Process(target=_worker, args=(barrier, str(silo), str(repo), f"w{i}"))
        for i in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)
    assert [w.exitcode for w in workers] == [0, 0, 0, 0]
    assert _ledger(silo)["notes"]["a.md"]["sessions"] == 200


def test_a_reader_without_lock_never_sees_a_truncated_ledger(home, repo, silo):
    seeded = {
        "notes": {
            f"n{i}.md": {"sessions": i, "last_session": "x", "last_read": "d"}
            for i in range(400)
        },
        "last_review": None,
    }
    (silo / LEDGER_NAME).write_text(json.dumps(seeded), encoding="utf-8")
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _LOOP, str(HOOK.parent), str(silo), str(repo), p],
            env={**os.environ, "HOME": str(home)},
        )
        for p in ("p", "q")
    ]
    reads: list[int] = []
    failures: list[str] = []

    def read_loop() -> None:
        while any(proc.poll() is None for proc in procs):
            try:
                reads.append(
                    len(json.loads((silo / LEDGER_NAME).read_bytes())["notes"])
                )
            except (ValueError, OSError) as exc:
                failures.append(repr(exc))

    reader = threading.Thread(target=read_loop)
    reader.start()
    assert [proc.wait(timeout=120) for proc in procs] == [0, 0]
    reader.join(timeout=10)
    assert failures == []
    assert len(reads) > 10
    assert set(reads) <= {400, 401}


# --- the lock and the marker --------------------------------------------------------------


def _hold_lock(directory: Path):
    handle = open(directory / LOCK_NAME, "a", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _release_lock(handle) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def test_the_marker_is_checked_under_the_lock(home, repo, silo):
    payload = json.dumps(_read(silo / "a.md", repo, "s1"))
    lock = _hold_lock(silo)
    proc = subprocess.Popen(
        [sys.executable, str(HOOK)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "HOME": str(home)},
    )
    assert proc.stdin is not None
    proc.stdin.write(payload)
    proc.stdin.close()
    time.sleep(0.4)
    assert proc.poll() is None
    (silo / MARKER_NAME).write_text("", encoding="utf-8")
    _release_lock(lock)
    stdout = proc.stdout.read() if proc.stdout else ""
    assert (proc.wait(timeout=10), stdout) == (0, "")
    assert not (silo / LEDGER_NAME).exists()


def test_a_lock_held_elsewhere_does_not_block_the_hook(home, repo, silo):
    cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1)
    before = (silo / LEDGER_NAME).read_bytes()
    lock = _hold_lock(silo)
    try:
        start = time.monotonic()
        result = _run(json.dumps(_read(silo / "a.md", repo, "s2")), home)
        elapsed = time.monotonic() - start
    finally:
        _release_lock(lock)
    assert (result.returncode, result.stdout) == (0, "")
    assert result.stderr == "memory_count: ledger lock still held, nothing counted\n"
    assert 0.9 <= elapsed < 3
    assert (silo / LEDGER_NAME).read_bytes() == before


def test_a_failure_between_the_temp_file_and_the_replace_leaves_no_temp(
    home, repo, silo, monkeypatch
):
    def refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(cnt.os, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        cnt.record_read(_read(silo / "a.md", repo, "s1"), DAY1)
    monkeypatch.undo()
    assert sorted(p.name for p in silo.iterdir()) == sorted(
        [LOCK_NAME, "MEMORY.md", "a.md"]
    )
