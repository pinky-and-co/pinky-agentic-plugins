#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/hooks/orchestrator_freeze.py — run with:
python3 -m unittest discover tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_PLUGIN = Path(__file__).parent.parent / "plugins" / "pc-kanban"
sys.path.insert(0, str(_PLUGIN / "hooks"))
sys.path.insert(0, str(_PLUGIN / "skills" / "permission-change" / "scripts"))

import guard  # noqa: E402
import orchestrator_freeze as of  # noqa: E402


class TestApplyOrchestratorScope(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.ws = self.base / "ws"
        (self.ws / "_KANBAN").mkdir(parents=True)
        self.agentlog = self.ws / "_AGENT_LOG"

    def tearDown(self):
        self._tmp.cleanup()

    def _apply(self, cwd: Path) -> str | None:
        """Run the hook body with the session bound to `cwd` — CLAUDE_PROJECT_DIR == cwd, as it
        is when a session opens at a directory (and it keeps board resolution off the real
        machine's boards)."""
        payload = {
            "hook_event_name": "SessionStart",
            "session_id": "t",
            "cwd": str(cwd),
        }
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(cwd)}):
            return of.apply_orchestrator_scope(payload)

    def _state(self, cwd: Path) -> dict:
        return guard.load_state(guard.state_path(cwd))

    def test_out_of_repo_with_a_board_freezes_to_board_dirs(self):
        summary = self._apply(self.ws)
        self.assertIsNotNone(summary)
        state = self._state(self.ws)
        self.assertTrue(state["careful"])
        self.assertEqual(
            sorted(state["freeze"]),
            sorted([str(self.ws / "_KANBAN"), str(self.agentlog)]),
        )

    def test_inside_a_git_repo_is_untouched(self):
        repo = self.ws / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.assertIsNone(self._apply(repo))
        self.assertEqual(self._state(repo)["freeze"], [])

    def test_no_board_mounted_is_untouched(self):
        bare = self.base / "bare"  # no _KANBAN at or above it
        bare.mkdir()
        self.assertIsNone(self._apply(bare))
        self.assertEqual(self._state(bare)["freeze"], [])

    def test_idempotent_second_run_does_not_rewrite(self):
        self.assertIsNotNone(self._apply(self.ws))
        self.assertIsNone(self._apply(self.ws))

    def test_existing_deny_list_is_preserved(self):
        guard.save_state(
            guard.state_path(self.ws),
            {"careful": False, "freeze": [], "deny": [str(self.base / "secret")]},
        )
        self._apply(self.ws)
        self.assertEqual(self._state(self.ws)["deny"], [str(self.base / "secret")])


class TestInGitRepo(unittest.TestCase):
    def test_true_inside_a_repo_false_outside(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            outside = root / "outside"
            outside.mkdir()
            self.assertFalse(of.in_git_repo(outside))
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            self.assertTrue(of.in_git_repo(repo))
            sub = repo / "src"
            sub.mkdir()
            self.assertTrue(of.in_git_repo(sub))


class TestMainEndToEnd(unittest.TestCase):
    SCRIPT = str(_PLUGIN / "hooks" / "orchestrator_freeze.py")

    def test_emits_sessionstart_envelope_and_writes_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve() / "ws"
            (ws / "_KANBAN").mkdir(parents=True)
            result = subprocess.run(
                [sys.executable, self.SCRIPT],
                input=json.dumps({"hook_event_name": "SessionStart", "cwd": str(ws)}),
                capture_output=True,
                text=True,
                env={"CLAUDE_PROJECT_DIR": str(ws), "PATH": "/usr/bin:/bin"},
                check=False,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(
                json.loads(result.stdout)["hookSpecificOutput"]["hookEventName"],
                "SessionStart",
            )
            state = guard.load_state(guard.state_path(ws))
            self.assertTrue(state["careful"])
            self.assertIn(str(ws / "_KANBAN"), state["freeze"])


if __name__ == "__main__":
    unittest.main()
