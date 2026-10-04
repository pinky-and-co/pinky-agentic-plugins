#!/usr/bin/env python3
"""Tests for plugins/pc-autodiscovery/hooks/sync_plugin_scope.py — run with:
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
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

HOOKS = Path(__file__).parent.parent / "plugins" / "pc-autodiscovery" / "hooks"
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


class _AscendBase(unittest.TestCase):
    """Ascend counterpart of `_Base`: `activate_for_path(descent=False)` is
    the SessionStart path — it installs at project scope, in the repo opened
    in (`session_dir` == the path being activated for). Only
    `install_plugin_project` (the `claude` CLI) is faked, same boundary as
    `_Base`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project_dir = self.root / "repo"
        (self.project_dir / ".claude").mkdir(parents=True)

        self.installed = self.root / "installed_plugins.json"
        self._orig_installed = psl.INSTALLED_PLUGINS_PATH
        psl.INSTALLED_PLUGINS_PATH = self.installed

        self.install_calls: list[tuple[str, Path]] = []
        self._install_outcome = psl.EnableResult(True, False, "")
        self._orig_install = sps.install_plugin_project
        sps.install_plugin_project = self._fake_install

    def tearDown(self):
        psl.INSTALLED_PLUGINS_PATH = self._orig_installed
        sps.install_plugin_project = self._orig_install
        self._tmp.cleanup()

    def _fake_install(self, plugin_id: str, *, cwd: Path) -> psl.EnableResult:
        self.install_calls.append((plugin_id, cwd))
        return self._install_outcome

    def _write_installed(self, plugins: dict) -> None:
        self.installed.write_text(json.dumps({"plugins": plugins}))

    def _write_settings_json(self, enabled: dict) -> None:
        (self.project_dir / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": enabled})
        )

    def _run(self, state=None):
        state = state or {"activated": []}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            newly = sps.activate_for_path(
                self.project_dir, state, session_dir=self.project_dir, descent=False
            )
        return newly, state, err.getvalue()


class TestActivateForPathAscend(_AscendBase):
    def test_installs_at_project_scope_a_plugin_scoped_to_an_ancestor(self):
        self._write_installed(
            {"pc-snow@mp": [{"scope": "project", "projectPath": str(self.root)}]}
        )
        newly, state, _ = self._run()
        self.assertEqual(self.install_calls, [("pc-snow@mp", self.project_dir)])
        self.assertEqual(newly, ["pc-snow@mp"])
        self.assertEqual(state["activated"], ["pc-snow@mp"])

    def test_skips_a_plugin_already_listed_in_settings_json(self):
        self._write_installed(
            {"pc-snow@mp": [{"scope": "project", "projectPath": str(self.root)}]}
        )
        self._write_settings_json({"pc-snow@mp": True})
        newly, _, _ = self._run()
        self.assertEqual(self.install_calls, [])
        self.assertEqual(newly, [])

    def test_skips_a_plugin_already_project_scope_installed_here(self):
        # projectPath == ce repo (pas un ancêtre) → déjà installé, à sauter même sans activation déclarée.
        self._write_installed(
            {"pc-snow@mp": [{"scope": "project", "projectPath": str(self.project_dir)}]}
        )
        newly, _, _ = self._run()
        self.assertEqual(self.install_calls, [])
        self.assertEqual(newly, [])


class TestInstallPluginProject(unittest.TestCase):
    """Oracle for the exact `claude` invocation the ascend relies on — same
    argv shape as `spawn-workers`' `enable_project_plugins`."""

    def test_argv_and_cwd_are_exact(self):
        completed = unittest.mock.Mock(returncode=0, stderr="")
        with unittest.mock.patch("subprocess.run", return_value=completed) as run:
            result = psl.install_plugin_project("pc-snow@mp", cwd=Path("/repo"))
        run.assert_called_once_with(
            [
                "claude",
                "plugin",
                "install",
                "pc-snow@mp",
                "--scope",
                "project",
                "--yes",
            ],
            cwd=Path("/repo"),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertTrue(result.success)


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


class TestHandleSessionEndScopeIsolation(unittest.TestCase):
    """SessionEnd restores the descent's settings.local.json only — the
    ascend's project-scope settings.json (a versioned file, meant to
    persist) must come back untouched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig_psl_dir, psl.STATE_DIR = psl.STATE_DIR, root / "scope"
        self._orig_pua_dir, pua.STATE_DIR = pua.STATE_DIR, root / "pending"
        self.session = "sess-end"
        self.project_dir = root / "repo"
        (self.project_dir / ".claude").mkdir(parents=True)

        self.settings_local = self.project_dir / ".claude" / "settings.local.json"
        self.settings_local.write_text(
            json.dumps({"enabledPlugins": {"post-cd@mp": True}})
        )
        psl.backup_settings_local_shared(self.settings_local, self.session, os.getpid())
        # l'état local diverge maintenant de la sauvegarde, comme si CwdChanged avait écrit
        self.settings_local.write_text(
            json.dumps({"enabledPlugins": {"post-cd@mp": True, "extra@mp": True}})
        )

        self.settings_json = self.project_dir / ".claude" / "settings.json"
        self.settings_json.write_text(
            json.dumps({"enabledPlugins": {"pc-snow@mp": True}})
        )

        psl.save_state(
            self.session,
            {
                "activated": ["pc-snow@mp"],
                "settings_local_path": str(self.settings_local),
            },
        )

    def tearDown(self):
        psl.STATE_DIR = self._orig_psl_dir
        pua.STATE_DIR = self._orig_pua_dir
        self._tmp.cleanup()

    def test_settings_json_survives_session_end_untouched(self):
        before = self.settings_json.read_text()
        sps.handle_session_end({"session_id": self.session})
        self.assertEqual(self.settings_json.read_text(), before)

    def test_settings_local_is_restored_to_its_pre_ascend_content(self):
        sps.handle_session_end({"session_id": self.session})
        restored = json.loads(self.settings_local.read_text())
        self.assertEqual(restored, {"enabledPlugins": {"post-cd@mp": True}})


class TestHandleSessionEndNeverWroteSettingsLocal(unittest.TestCase):
    """The common post-fix case: the ascend only installed at project scope
    (settings.json), CwdChanged never fired, so settings.local.json was
    never created. SessionEnd must not crash, and settings.json — a
    versioned file — must survive untouched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig_psl_dir, psl.STATE_DIR = psl.STATE_DIR, root / "scope"
        self._orig_pua_dir, pua.STATE_DIR = pua.STATE_DIR, root / "pending"
        self.session = "sess-end-no-local"
        self.project_dir = root / "repo"
        (self.project_dir / ".claude").mkdir(parents=True)

        self.settings_local = self.project_dir / ".claude" / "settings.local.json"
        psl.backup_settings_local_shared(
            self.settings_local, self.session, os.getpid()
        )  # n'a jamais existé

        self.settings_json = self.project_dir / ".claude" / "settings.json"
        self.settings_json.write_text(
            json.dumps({"enabledPlugins": {"pc-snow@mp": True}})
        )

        psl.save_state(
            self.session,
            {
                "activated": ["pc-snow@mp"],
                "settings_local_path": str(self.settings_local),
            },
        )

    def tearDown(self):
        psl.STATE_DIR = self._orig_psl_dir
        pua.STATE_DIR = self._orig_pua_dir
        self._tmp.cleanup()

    def test_settings_json_survives_and_no_local_file_is_created(self):
        before = self.settings_json.read_text()
        sps.handle_session_end({"session_id": self.session})
        self.assertEqual(self.settings_json.read_text(), before)
        self.assertFalse(self.settings_local.exists())


class TestSharedBackupConcurrentSessions(unittest.TestCase):
    """Done when #1/#3/#4: the backup is shared per directory, not per
    session — the second session to arrive joins it instead of recapturing
    an already-modified file, and restore fires only once every session
    (live or crashed) has left."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig_psl_dir, psl.STATE_DIR = psl.STATE_DIR, root / "scope"
        self.project_dir = root / "repo"
        (self.project_dir / ".claude").mkdir(parents=True)
        self.settings_local = self.project_dir / ".claude" / "settings.local.json"
        self.settings_local.write_text(
            json.dumps({"enabledPlugins": {"orig@mp": True}})
        )

    def tearDown(self):
        psl.STATE_DIR = self._orig_psl_dir
        self._tmp.cleanup()

    def _open_two_sessions(self):
        """Session A opens first and captures the backup; session B opens
        second, on the already-open directory, and must join without
        recapturing. Both use this test process's own (genuinely alive) PID."""
        psl.backup_settings_local_shared(self.settings_local, "A", os.getpid())
        psl.backup_settings_local_shared(self.settings_local, "B", os.getpid())
        # une session ultérieure écrit (CwdChanged) — l'état diverge de l'original
        self.settings_local.write_text(
            json.dumps({"enabledPlugins": {"orig@mp": True, "cwd-written@mp": True}})
        )

    def test_lifo_close_restores_the_original_once_both_are_gone(self):
        self._open_two_sessions()
        psl.restore_settings_local_shared(
            self.settings_local, "B"
        )  # dernière ouverte, ferme en 1er
        self.assertEqual(
            json.loads(self.settings_local.read_text())["enabledPlugins"],
            {"orig@mp": True, "cwd-written@mp": True},
        )  # A encore vivante — pas de restauration
        psl.restore_settings_local_shared(self.settings_local, "A")
        self.assertEqual(
            json.loads(self.settings_local.read_text()),
            {"enabledPlugins": {"orig@mp": True}},
        )

    def test_non_lifo_close_restores_the_original_once_both_are_gone(self):
        self._open_two_sessions()
        psl.restore_settings_local_shared(
            self.settings_local, "A"
        )  # ouverte en 1er, ferme en 1er
        self.assertEqual(
            json.loads(self.settings_local.read_text())["enabledPlugins"],
            {"orig@mp": True, "cwd-written@mp": True},
        )  # B encore vivante — pas de restauration
        psl.restore_settings_local_shared(self.settings_local, "B")
        self.assertEqual(
            json.loads(self.settings_local.read_text()),
            {"enabledPlugins": {"orig@mp": True}},
        )

    def test_a_crashed_sessions_dead_pid_is_purged_and_restore_still_fires(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"]
        )  # portable, pas de "true" sous Windows
        dead_pid = proc.pid
        proc.wait()  # garanti mort : le fd n'a jamais été relâché explicitement, juste fermé

        psl.backup_settings_local_shared(self.settings_local, "crashed", dead_pid)
        psl.backup_settings_local_shared(self.settings_local, "alive", os.getpid())
        self.settings_local.write_text(
            json.dumps({"enabledPlugins": {"orig@mp": True, "cwd-written@mp": True}})
        )

        # la session vivante ferme normalement — le PID de la session crashée est
        # détecté mort au passage, purgé, et plus aucun verrou vivant ne reste
        psl.restore_settings_local_shared(self.settings_local, "alive")
        self.assertEqual(
            json.loads(self.settings_local.read_text()),
            {"enabledPlugins": {"orig@mp": True}},
        )


if __name__ == "__main__":
    unittest.main()
