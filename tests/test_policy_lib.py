#!/usr/bin/env python3
"""Tests for plugins/pc-core/hooks/policy_lib.py — run with:
python3 -m unittest discover tests
"""

import json
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
            (root / ".claude" / "settings.pinky.json").write_text(
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
            (root / ".claude" / "settings.pinky.json").write_text("{not json")
            self.assertIsNone(pl.load_policies(root))


class TestLoadPoliciesCascade(unittest.TestCase):
    """Every key inherits from the nearest ancestor if the repo omits it — no per-key exception
    (branch_workflow included, deliberately not special-cased)."""

    def _write(self, path: Path, data: dict) -> None:
        (path / ".claude").mkdir(parents=True, exist_ok=True)
        (path / ".claude" / "settings.pinky.json").write_text(json.dumps(data))

    def test_repo_key_wins_over_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, repo = Path(tmp), Path(tmp) / "repo"
            repo.mkdir()
            self._write(root, {"language": "fr"})
            self._write(repo, {"language": "en"})
            self.assertEqual(pl.load_policies(repo)["language"], "en")

    def test_falls_back_to_ancestor_when_repo_omits_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, repo = Path(tmp), Path(tmp) / "repo"
            repo.mkdir()
            self._write(root, {"branch_workflow": "git flow"})
            self._write(repo, {"language": "en"})
            merged = pl.load_policies(repo)
            self.assertEqual(merged["language"], "en")
            self.assertEqual(
                merged["branch_workflow"], "git flow"
            )  # no exception for this key

    def test_nested_two_levels_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            group = root / "group"
            repo = group / "repo"
            repo.mkdir(parents=True)
            self._write(root, {"language": "fr"})
            self.assertEqual(pl.load_policies(repo)["language"], "fr")


if __name__ == "__main__":
    unittest.main()
