"""Tests for `index_note.py`, which indexes a note the `memory-hold` mod moved to another level."""

import subprocess
import sys
from pathlib import Path

PLUGIN = Path(__file__).parent.parent / "plugins" / "pc-memory"
SCRIPT = PLUGIN / "hooks" / "memory-hold" / "index_note.py"
sys.path.insert(0, str(SCRIPT.parent))

import index_note  # noqa: E402

NOTE = "---\nname: zz-probe\ndescription: Une sonde\nmetadata:\n  type: project\n---\n\nprobe\n"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_build_index_line_from_the_frontmatter():
    assert (
        index_note.build_index_line("sub/a.md", NOTE)
        == "- [zz-probe](sub/a.md) — Une sonde"
    )


def test_build_index_line_without_frontmatter_names_the_file():
    assert index_note.build_index_line("a.md", "just a body\n") == "- [a](a.md)"


def test_appends_to_an_index_missing_its_last_newline(tmp_path):
    (tmp_path / "a.md").write_text(NOTE)
    (tmp_path / "MEMORY.md").write_text("- [old](old.md) — kept")
    out = _run(str(tmp_path), "a.md")
    assert (out.returncode, out.stdout) == (0, "indexed\n")
    assert (tmp_path / "MEMORY.md").read_text() == (
        "- [old](old.md) — kept\n- [zz-probe](a.md) — Une sonde\n"
    )


def test_creates_the_index_when_missing(tmp_path):
    (tmp_path / "a.md").write_text(NOTE)
    assert _run(str(tmp_path), "a.md").returncode == 0
    assert (tmp_path / "MEMORY.md").read_text() == "- [zz-probe](a.md) — Une sonde\n"


def test_a_note_already_linked_is_not_added_twice(tmp_path):
    (tmp_path / "a.md").write_text(NOTE)
    (tmp_path / "MEMORY.md").write_text("- [other title](./a.md) — x\n")
    out = _run(str(tmp_path), "a.md")
    assert (out.returncode, out.stdout) == (0, "already indexed\n")
    assert (tmp_path / "MEMORY.md").read_text() == "- [other title](./a.md) — x\n"


def test_a_missing_note_exits_1_and_leaves_no_index(tmp_path):
    out = _run(str(tmp_path), "a.md")
    assert out.returncode == 1
    assert not (tmp_path / "MEMORY.md").exists()


def test_a_wrong_call_exits_2():
    assert _run("only-one").returncode == 2
