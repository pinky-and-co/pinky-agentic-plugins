#!/usr/bin/env python3
"""Tests for plugins/pc-core/hooks/policy_lib.py — run with:
python3 -m unittest discover tests
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "hooks"))

import policy_lib as pl


class TestResolveRepoRoot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolves_real_git_repo_root(self):
        nested = self.repo / "a" / "b"
        nested.mkdir(parents=True)
        resolved = pl.resolve_repo_root(str(self.repo), str(nested / "file.py"))
        self.assertEqual(resolved.resolve(), self.repo.resolve())

    def test_falls_back_to_project_dir_outside_any_repo(self):
        with tempfile.TemporaryDirectory() as outside:
            resolved = pl.resolve_repo_root(outside, None)
            self.assertEqual(resolved, Path(outside))


class TestLoadPolicies(unittest.TestCase):
    def test_loads_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude").mkdir()
            (root / ".claude" / "policies.json").write_text(
                '{"branch_workflow": "git flow"}'
            )
            self.assertEqual(pl.load_policies(root), {"branch_workflow": "git flow"})

    def test_returns_none_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(pl.load_policies(Path(tmp)))

    def test_returns_none_on_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude").mkdir()
            (root / ".claude" / "policies.json").write_text("{not json")
            self.assertIsNone(pl.load_policies(root))


if __name__ == "__main__":
    unittest.main()
