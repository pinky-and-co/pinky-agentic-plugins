#!/usr/bin/env python3
"""Tests for plugins/pc-delegate/mcp/subtree.py

Defines "done": given a touched `.py` file, the returned subtree is exactly that file plus the
modules it directly imports — via a real `grimp` graph when the file sits in a proper package
(`__init__.py` present), via a direct-import AST scan over sibling files otherwise (this
workspace's own `scripts/`/`skills/*/scripts/` layout) — never transitive, never a file outside
the repo root, and a non-`.py` touched file passes through with no dependency expansion attempted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).parent.parent / "plugins" / "pc-delegate" / "mcp"
sys.path.insert(0, str(MCP_DIR))

import subtree as st  # noqa: E402


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestPackageStrategy:
    def setup_method(self) -> None:
        pytest.importorskip("grimp")

    def test_direct_import_within_package_is_included(self, tmp_path: Path) -> None:
        repo = tmp_path
        _write(repo / "pkg" / "__init__.py")
        _write(repo / "pkg" / "a.py", "from pkg import b\n")
        _write(repo / "pkg" / "b.py", "x = 1\n")
        _write(repo / "pkg" / "c.py", "x = 2\n")

        result = st.compute_subtree(repo, ["pkg/a.py"])
        assert result == ["pkg/a.py", "pkg/b.py"]

    def test_repo_root_itself_is_the_package(self, tmp_path: Path) -> None:
        repo = tmp_path / "myrepo"
        repo.mkdir()
        _write(repo / "__init__.py")
        _write(repo / "a.py", "from myrepo import b\n")
        _write(repo / "b.py", "x = 1\n")

        result = st.compute_subtree(repo, ["a.py"])
        assert result == ["a.py", "b.py"]

    def test_grimp_failure_is_wrapped_as_subtree_error(self, tmp_path: Path) -> None:
        repo = tmp_path
        _write(repo / "pkg" / "__init__.py", "this is not valid python (((\n")
        _write(repo / "pkg" / "a.py", "x = 1\n")
        with pytest.raises(st.SubtreeError):
            st.compute_subtree(repo, ["pkg/a.py"])

    def test_transitive_import_is_not_included(self, tmp_path: Path) -> None:
        repo = tmp_path
        _write(repo / "pkg" / "__init__.py")
        _write(repo / "pkg" / "a.py", "from pkg import b\n")
        _write(repo / "pkg" / "b.py", "from pkg import c\n")
        _write(repo / "pkg" / "c.py", "x = 1\n")

        result = st.compute_subtree(repo, ["pkg/a.py"])
        assert result == ["pkg/a.py", "pkg/b.py"]
        assert "pkg/c.py" not in result


class TestFlatDirectoryFallback:
    def test_sibling_import_with_no_init_py_is_included(self, tmp_path: Path) -> None:
        repo = tmp_path
        _write(repo / "scripts" / "main.py", "import helper\n")
        _write(repo / "scripts" / "helper.py", "x = 1\n")
        _write(repo / "scripts" / "unrelated.py", "x = 2\n")

        result = st.compute_subtree(repo, ["scripts/main.py"])
        assert result == ["scripts/helper.py", "scripts/main.py"]

    def test_from_import_sibling_is_included(self, tmp_path: Path) -> None:
        repo = tmp_path
        _write(repo / "scripts" / "main.py", "from helper import thing\n")
        _write(repo / "scripts" / "helper.py", "thing = 1\n")

        result = st.compute_subtree(repo, ["scripts/main.py"])
        assert result == ["scripts/helper.py", "scripts/main.py"]

    def test_stdlib_import_has_no_sibling_match(self, tmp_path: Path) -> None:
        repo = tmp_path
        _write(repo / "scripts" / "main.py", "import os\nimport sys\n")

        result = st.compute_subtree(repo, ["scripts/main.py"])
        assert result == ["scripts/main.py"]


class TestNonPythonAndErrors:
    def test_non_python_touched_file_passes_through_unexpanded(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path
        _write(repo / "README.md", "# hi\n")

        result = st.compute_subtree(repo, ["README.md"])
        assert result == ["README.md"]

    def test_multiple_touched_files_are_all_included(self, tmp_path: Path) -> None:
        repo = tmp_path
        _write(repo / "scripts" / "a.py", "import b\n")
        _write(repo / "scripts" / "b.py", "x = 1\n")
        _write(repo / "docs.md", "# doc\n")

        result = st.compute_subtree(repo, ["scripts/a.py", "docs.md"])
        assert result == ["docs.md", "scripts/a.py", "scripts/b.py"]

    def test_missing_repo_raises(self, tmp_path: Path) -> None:
        with pytest.raises(st.SubtreeError):
            st.compute_subtree(tmp_path / "does_not_exist", ["a.py"])

    def test_missing_touched_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(st.SubtreeError):
            st.compute_subtree(tmp_path, ["nope.py"])

    def test_touched_file_escaping_repo_raises(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        with pytest.raises(st.SubtreeError):
            st.compute_subtree(repo, ["../outside.py"])
