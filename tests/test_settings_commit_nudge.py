#!/usr/bin/env python3
"""Tests for plugins/pc-autodiscovery/hooks/settings_commit_nudge.py — run with:
python3 -m unittest discover tests

Defines "done": the SessionStart hook emits an `additionalContext` line iff the
session's repo has exactly one uncommitted change — `enabledPlugins` entries in
`.claude/settings.json` and nothing else. A clean tree, a wider diff, or a
settings.json change touching any other key stays silent. Not a git repo →
silent. `git` runs for real against a tmp repo; only the outside world is real.
"""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HOOKS = Path(__file__).parent.parent / "plugins" / "pc-autodiscovery" / "hooks"
sys.path.insert(0, str(HOOKS))

import settings_commit_nudge as nudge  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class TestSettingsCommitNudge(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@t.t")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / ".claude").mkdir()
        self._write_settings({"$schema": "x", "enabledPlugins": {"pc-core@mp": True}})
        (self.repo / "README.md").write_text("hi\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "init")

    def tearDown(self):
        self._tmp.cleanup()

    def _write_settings(self, data: dict) -> None:
        (self.repo / ".claude" / "settings.json").write_text(json.dumps(data, indent=2))

    def _run(self) -> dict:
        out = io.StringIO()
        sys.stdin = io.StringIO(json.dumps({"session_id": "s", "cwd": str(self.repo)}))
        try:
            with redirect_stdout(out):
                nudge.main()
        finally:
            sys.stdin = sys.__stdin__
        return json.loads(out.getvalue())

    def _ctx(self, result: dict) -> str | None:
        return result["hookSpecificOutput"].get("additionalContext")

    def test_clean_tree_is_silent(self):
        self.assertIsNone(self._ctx(self._run()))

    def test_enabled_plugins_only_change_nudges(self):
        self._write_settings(
            {"$schema": "x", "enabledPlugins": {"pc-core@mp": True, "pc-git@mp": True}}
        )
        ctx = self._ctx(self._run())
        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertIn(".claude/settings.json", ctx)

    def test_a_wider_diff_stays_silent(self):
        self._write_settings(
            {"$schema": "x", "enabledPlugins": {"pc-core@mp": True, "pc-git@mp": True}}
        )
        (self.repo / "README.md").write_text("changed\n")
        self.assertIsNone(self._ctx(self._run()))

    def test_a_non_plugin_settings_key_change_stays_silent(self):
        self._write_settings(
            {"$schema": "x", "enabledPlugins": {"pc-core@mp": True}, "model": "opus"}
        )
        self.assertIsNone(self._ctx(self._run()))

    def test_not_a_git_repo_is_silent(self):
        with tempfile.TemporaryDirectory() as plain:
            sys.stdin = io.StringIO(json.dumps({"session_id": "s", "cwd": plain}))
            out = io.StringIO()
            try:
                with redirect_stdout(out):
                    nudge.main()
            finally:
                sys.stdin = sys.__stdin__
            self.assertIsNone(self._ctx(json.loads(out.getvalue())))

    def test_staged_plugin_change_also_nudges(self):
        self._write_settings(
            {"$schema": "x", "enabledPlugins": {"pc-core@mp": True, "pc-git@mp": True}}
        )
        _git(self.repo, "add", ".claude/settings.json")
        self.assertIsNotNone(self._ctx(self._run()))


if __name__ == "__main__":
    unittest.main()
