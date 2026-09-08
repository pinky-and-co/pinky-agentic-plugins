#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/skills/chantier/scripts/activate_plugins_for_repos.py
— run with: python3 -m unittest discover tests

Defines "done": given resolved card repo paths, `main()` runs `claude plugin
enable -s local` once per project-scoped plugin that matches a path and is
not already on (settings.local.json or a previous enable this call), prints
the newly-enabled ids to stdout, and treats an "already enabled" result as a
stderr note — never a failure. User-scoped plugins and plugins the
SessionStart ascend already covered are skipped by `matching_plugin_ids`.

`matching_plugin_ids` / `settings_local_plugin_ids` run for real against
fixtures; only `enable_plugin_local` (the `claude` CLI) is faked.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-kanban"
    / "skills"
    / "chantier"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

import plugin_scope_lib as psl  # noqa: E402
import activate_plugins_for_repos as apr  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.open_path = self.root / "ws"
        (self.open_path / ".claude").mkdir(parents=True)
        self.card_repo = self.open_path / "pinky-lib" / "pinky-snowpark"
        self.card_repo.mkdir(parents=True)

        self.installed = self.root / "installed_plugins.json"
        self._orig_installed = psl.INSTALLED_PLUGINS_PATH
        psl.INSTALLED_PLUGINS_PATH = self.installed

        self._prev_env = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.open_path)

        self.enable_calls: list[str] = []
        self._enable_outcome = psl.EnableResult(True, False, "")
        self._orig_enable = apr.enable_plugin_local
        apr.enable_plugin_local = self._fake_enable

    def tearDown(self):
        psl.INSTALLED_PLUGINS_PATH = self._orig_installed
        apr.enable_plugin_local = self._orig_enable
        if self._prev_env is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = self._prev_env
        self._tmp.cleanup()

    def _fake_enable(self, plugin_id: str) -> psl.EnableResult:
        self.enable_calls.append(plugin_id)
        return self._enable_outcome

    def _write_installed(self, plugins: dict) -> None:
        self.installed.write_text(json.dumps({"plugins": plugins}))

    def _write_settings_local(self, enabled: dict) -> None:
        (self.open_path / ".claude" / "settings.local.json").write_text(
            json.dumps({"enabledPlugins": enabled})
        )

    def _run(self, *paths: str):
        sys.argv = ["activate_plugins_for_repos.py", *(paths or [str(self.card_repo)])]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = apr.main()
        return rc, out.getvalue(), err.getvalue()


class TestActivatePluginsForRepos(_Base):
    def test_enables_a_matching_project_plugin_and_prints_it(self):
        self._write_installed(
            {"pc-snow@mp": [{"scope": "project", "projectPath": str(self.card_repo)}]}
        )
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self.enable_calls, ["pc-snow@mp"])
        self.assertEqual(out.strip(), "pc-snow@mp")

    def test_skips_a_plugin_already_in_settings_local(self):
        self._write_installed(
            {"pc-snow@mp": [{"scope": "project", "projectPath": str(self.card_repo)}]}
        )
        self._write_settings_local({"pc-snow@mp": True})
        _, out, _ = self._run()
        self.assertEqual(self.enable_calls, [])
        self.assertEqual(out.strip(), "")

    def test_skips_a_user_scoped_plugin(self):
        self._write_installed(
            {
                "pc-core@mp": [
                    {"scope": "user"},
                    {"scope": "project", "projectPath": str(self.card_repo)},
                ]
            }
        )
        _, out, _ = self._run()
        self.assertEqual(self.enable_calls, [])
        self.assertEqual(out.strip(), "")

    def test_already_enabled_is_a_stderr_note_not_a_failure(self):
        self._write_installed(
            {"pc-snow@mp": [{"scope": "project", "projectPath": str(self.card_repo)}]}
        )
        self._enable_outcome = psl.EnableResult(
            False, True, "already enabled at local scope"
        )
        rc, out, err = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")
        self.assertIn("already enabled", err)
        self.assertNotIn("failed to enable", err)

    def test_real_failure_is_reported_on_stderr(self):
        self._write_installed(
            {"pc-snow@mp": [{"scope": "project", "projectPath": str(self.card_repo)}]}
        )
        self._enable_outcome = psl.EnableResult(False, False, "marketplace unreachable")
        _, out, err = self._run()
        self.assertEqual(out.strip(), "")
        self.assertIn("failed to enable pc-snow@mp", err)

    def test_same_plugin_matched_by_two_paths_is_enabled_once(self):
        other = self.open_path / "pinky-lib" / "pinky-connect"
        other.mkdir(parents=True)
        self._write_installed(
            {
                "pc-snow@mp": [
                    {"scope": "project", "projectPath": str(self.card_repo)},
                    {"scope": "project", "projectPath": str(other)},
                ]
            }
        )
        _, out, _ = self._run(str(self.card_repo), str(other))
        self.assertEqual(self.enable_calls, ["pc-snow@mp"])
        self.assertEqual(out.strip(), "pc-snow@mp")

    def test_no_paths_returns_usage_error(self):
        sys.argv = ["activate_plugins_for_repos.py"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = apr.main()
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
