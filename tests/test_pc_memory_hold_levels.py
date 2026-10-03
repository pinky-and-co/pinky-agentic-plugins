"""Tests for `levels.py`, the levels the `memory-hold` mod offers for a held note."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "pc-memory"
SCRIPT = PLUGIN / "hooks" / "memory-hold" / "levels.py"
sys.path.insert(0, str(PLUGIN / "hooks"))

from pc_memory_lib import memory_dir_of  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Fake home with a `.claude/projects`, set as `HOME`, no ambient config dir."""
    root = tmp_path.resolve()
    (root / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return root


@pytest.fixture
def repo(home):
    """home/ws/repo, a git root, with an index at the `ws` level."""
    path = home / "ws" / "repo"
    (path / ".git").mkdir(parents=True)
    parent = memory_dir_of(home / "ws")
    parent.mkdir(parents=True)
    (parent / "MEMORY.md").write_text("")
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        check=False,
    )


def test_lists_current_then_indexed_parent_and_marks_the_target(home, repo):
    note = memory_dir_of(repo) / "sub" / "a.md"
    out = _run(str(repo), str(note))
    assert out.returncode == 0
    assert json.loads(out.stdout) == {
        "levels": [
            {
                "name": "repo",
                "dir": str(memory_dir_of(repo)),
                "current": True,
                "target": True,
            },
            {
                "name": "ws",
                "dir": str(memory_dir_of(home / "ws")),
                "current": False,
                "target": False,
            },
        ],
        "relative": "sub/a.md",
    }


def test_a_note_aimed_at_the_parent_targets_it(home, repo):
    out = _run(str(repo), str(memory_dir_of(home / "ws") / "a.md"))
    data = json.loads(out.stdout)
    assert [level["target"] for level in data["levels"]] == [False, True]
    assert data["relative"] == "a.md"


def test_a_path_outside_every_level_has_no_target(home, repo):
    data = json.loads(_run(str(repo), str(home / "elsewhere" / "a.md")).stdout)
    assert [level["target"] for level in data["levels"]] == [False, False]
    assert data["relative"] is None


def test_a_wrong_call_exits_2():
    out = _run("only-one-argument")
    assert out.returncode == 2
    assert out.stdout == ""
