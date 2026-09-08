#!/usr/bin/env python3
"""Tests for plugins/pc-core/hooks/sync_plugin_scope.py — run with:
python3 -m unittest discover tests

Defines "done" for the CwdChanged descent: `activate_for_path` enables a
project-scoped plugin discovered below the session's open path *only* when it
is not already on (settings.local.json, session state, or `claude plugin
enable` reporting it already enabled). An "already enabled" result is a
warning, never a crash. `matching_plugin_ids` / `settings_local_plugin_ids`
run for real against fixtures; only `enable_plugin_local` (the `claude` CLI)
is faked — the boundary.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).parent.parent / "plugins" / "pc-core" / "hooks"
sys.path.insert(0, str(HOOKS))

import pending_user_action as pua  # noqa: E402
import plugin_scope_lib as psl  # noqa: E402
import sync_plugin_scope as sps  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.open_path = self.root / "ws"
        (self.open_path / ".claude").mkdir(parents=True)
        (self.open_path / "ops").mkdir()

        # installed_plugins.json — pointed at a fixture, never the real one
        self.installed = self.root / "installed_plugins.json"
        self._orig_installed = psl.INSTALLED_PLUGINS_PATH
        psl.INSTALLED_PLUGINS_PATH = self.installed

        # every enable_plugin_local call recorded; outcome set per test
        self.enable_calls: list[tuple[str, Path]] = []
        self._enable_outcome = psl.EnableResult(True, False, "")
        self._orig_enable = sps.enable_plugin_local
        sps.enable_plugin_local = self._fake_enable

    def tearDown(self):
        psl.INSTALLED_PLUGINS_PATH = self._orig_installed
        sps.enable_plugin_local = self._orig_enable
        self._tmp.cleanup()

    def _fake_enable(self, plugin_id: str, *, cwd: Path) -> psl.EnableResult:
        self.enable_calls.append((plugin_id, cwd))
        return self._enable_outcome

    def _write_installed(self, plugins: dict) -> None:
        self.installed.write_text(json.dumps({"plugins": plugins}))

    def _write_settings_local(self, enabled: dict) -> None:
        (self.open_path / ".claude" / "settings.local.json").write_text(
            json.dumps({"enabledPlugins": enabled})
        )

    def _run(self, state=None):
        state = state or {"activated": []}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            newly = sps.activate_for_path(
                self.open_path / "ops",
                state,
                session_dir=self.open_path,
                descent=True,
            )
        return newly, state, err.getvalue()


class TestActivateForPath(_Base):
    def test_enables_a_missing_project_plugin_below_the_open_path(self):
        self._write_installed(
            {
                "pc-snow@mp": [
                    {"scope": "project", "projectPath": str(self.open_path / "ops")}
                ]
            }
        )
        newly, state, _ = self._run()
        self.assertEqual(self.enable_calls, [("pc-snow@mp", self.open_path)])
        self.assertEqual(newly, ["pc-snow@mp"])
        self.assertEqual(state["activated"], ["pc-snow@mp"])

    def test_skips_a_plugin_already_listed_in_settings_local(self):
        self._write_installed(
            {
                "pc-snow@mp": [
                    {"scope": "project", "projectPath": str(self.open_path / "ops")}
                ]
            }
        )
        self._write_settings_local({"pc-snow@mp": True})
        newly, _, _ = self._run()
        self.assertEqual(self.enable_calls, [])
        self.assertEqual(newly, [])

    def test_skips_a_user_scoped_plugin(self):
        self._write_installed(
            {
                "pc-core@mp": [
                    {"scope": "user"},
                    {"scope": "project", "projectPath": str(self.open_path / "ops")},
                ]
            }
        )
        newly, _, _ = self._run()
        self.assertEqual(self.enable_calls, [])
        self.assertEqual(newly, [])

    def test_already_enabled_is_a_warning_not_a_failure(self):
        self._write_installed(
            {
                "pc-snow@mp": [
                    {"scope": "project", "projectPath": str(self.open_path / "ops")}
                ]
            }
        )
        self._enable_outcome = psl.EnableResult(
            False, True, "already enabled at local scope"
        )
        newly, state, err = self._run()
        self.assertEqual(newly, [])  # not counted as newly enabled
        self.assertEqual(state["activated"], ["pc-snow@mp"])  # noted, won't retry
        self.assertIn("already enabled", err)
        self.assertNotIn("failed to enable", err)

    def test_a_real_enable_failure_is_reported_and_not_recorded(self):
        self._write_installed(
            {
                "pc-snow@mp": [
                    {"scope": "project", "projectPath": str(self.open_path / "ops")}
                ]
            }
        )
        self._enable_outcome = psl.EnableResult(False, False, "marketplace unreachable")
        newly, state, err = self._run()
        self.assertEqual(newly, [])
        self.assertEqual(state["activated"], [])
        self.assertIn("failed to enable pc-snow@mp", err)


class TestSessionOpenPath(unittest.TestCase):
    def test_recovered_from_the_stored_settings_local_path(self):
        state = {"settings_local_path": "/x/ws/.claude/settings.local.json"}
        self.assertEqual(sps.session_open_path(state), Path("/x/ws"))

    def test_falls_back_to_claude_project_dir(self):
        state = {}
        prev = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = "/x/ws"
        try:
            self.assertEqual(sps.session_open_path(state), Path("/x/ws"))
        finally:
            if prev is None:
                del os.environ["CLAUDE_PROJECT_DIR"]
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = prev

    def test_none_when_nothing_is_known(self):
        prev = os.environ.pop("CLAUDE_PROJECT_DIR", None)
        try:
            self.assertIsNone(sps.session_open_path({}))
        finally:
            if prev is not None:
                os.environ["CLAUDE_PROJECT_DIR"] = prev


class TestHandleCwdChanged(unittest.TestCase):
    """The CwdChanged handler queues a /reload-plugins pending action when —
    and only when — activate_for_path reports something newly enabled."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig_psl_dir, psl.STATE_DIR = psl.STATE_DIR, root / "scope"
        self._orig_pua_dir, pua.STATE_DIR = pua.STATE_DIR, root / "pending"
        self.session = "sess-cwd"
        self.open_path = root / "ws"
        (self.open_path / "sub").mkdir(parents=True)
        psl.save_state(
            self.session,
            {
                "activated": [],
                "settings_local_path": str(
                    self.open_path / ".claude" / "settings.local.json"
                ),
                "existed": False,
            },
        )
        self._orig_activate = sps.activate_for_path

    def tearDown(self):
        psl.STATE_DIR = self._orig_psl_dir
        pua.STATE_DIR = self._orig_pua_dir
        sps.activate_for_path = self._orig_activate
        self._tmp.cleanup()

    def _payload(self):
        return {"session_id": self.session, "cwd": str(self.open_path / "sub")}

    def test_newly_enabled_queues_reload_plugins(self):
        sps.activate_for_path = lambda *a, **k: ["pc-snow@mp", "pc-pypi@mp"]
        sps.handle_cwd_changed(self._payload())
        queue = pua.pending(self.session)
        self.assertIn("reload-plugins", queue)
        self.assertIn("pc-snow@mp", queue["reload-plugins"])
        self.assertIn("/reload-plugins", queue["reload-plugins"])

    def test_nothing_new_queues_nothing(self):
        sps.activate_for_path = lambda *a, **k: []
        sps.handle_cwd_changed(self._payload())
        self.assertEqual(pua.pending(self.session), {})


if __name__ == "__main__":
    unittest.main()
