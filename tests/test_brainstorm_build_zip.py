"""Tests for plugins/pc-kanban/skills/brainstorm/scripts/build_zip.py."""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from pathlib import Path

SKILL_DIR = (
    Path(__file__).parent.parent / "plugins" / "pc-kanban" / "skills" / "brainstorm"
)
SCRIPT = SKILL_DIR / "scripts" / "build_zip.py"
SOURCE = SKILL_DIR / "SKILL.md"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_zip_holds_identical_skill(tmp_path: Path) -> None:
    out = tmp_path / "b.zip"
    result = _run("--out", str(out))
    assert result.returncode == 0
    with zipfile.ZipFile(out) as archive:
        assert archive.namelist() == ["brainstorm/SKILL.md"]
        assert archive.read("brainstorm/SKILL.md") == SOURCE.read_bytes()


def test_refuses_output_inside_repo() -> None:
    result = _run("--out", str(SKILL_DIR / "x.zip"))
    assert result.returncode == 2
    assert "refusing to write inside the repository" in result.stderr
    assert not (SKILL_DIR / "x.zip").exists()


def test_default_path_is_outside_repo_and_exists() -> None:
    result = _run()
    assert result.returncode == 0
    printed = Path(result.stdout.strip())
    assert printed.is_file()
    assert not printed.is_relative_to(SKILL_DIR.parents[3])


def test_body_has_no_claude_code_mechanism() -> None:
    body = SOURCE.read_text()
    assert not re.search(
        r"AskUserQuestion|Bash|CLAUDE_SKILL_DIR|MCP|CLAUDE_PLUGIN_ROOT|allowed-tools"
        r"|argument-hint|mcp__|SlashCommand|WebFetch|\$ARGUMENTS",
        body,
    )


def test_frontmatter_keys_are_name_and_description() -> None:
    frontmatter = SOURCE.read_text().split("---")[1]
    keys = {
        line.split(":", 1)[0]
        for line in frontmatter.splitlines()
        if re.match(r"^[A-Za-z_-]+:", line)
    }
    assert keys == {"name", "description"}
