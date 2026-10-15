#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/hooks/guard-state/pc_kanban_guard_env_hook.py — run with:
python3 -m unittest discover tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_PLUGIN = Path(__file__).parent.parent / "plugins" / "pc-kanban"
_HOOK_DIR = _PLUGIN / "hooks" / "guard-state"
sys.path.insert(0, str(_HOOK_DIR))

import pc_kanban_guard_env_hook as hook  # noqa: E402

HOOK = str(_HOOK_DIR / "pc_kanban_guard_env_hook.py")
PATH = "/usr/bin:/bin"
HOOK_NAME = "guard-state/pc_kanban_guard_env_hook.py"


def run_hook(env: dict, event: dict | None = None) -> subprocess.CompletedProcess:
    """Run the hook as the harness does: SessionStart JSON on stdin, a bare environment."""
    return subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(event if event is not None else {}),
        capture_output=True,
        text=True,
        env={"PATH": PATH, **env},
        check=False,
    )


class TestBuildExportLine(unittest.TestCase):
    def test_a_plain_root(self):
        self.assertEqual(
            hook.build_export_line(Path("/work/repo")),
            "export CLAUDE_PROJECT_DIR=/work/repo\n",
        )

    def test_a_root_with_a_space_is_shell_quoted(self):
        self.assertEqual(
            hook.build_export_line(Path("/work/my repo")),
            "export CLAUDE_PROJECT_DIR='/work/my repo'\n",
        )


class TestExportToEnvFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.env_file = self.base / "env.sh"
        self.root = self.base / "repo"
        self.root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_fr_0025_396_the_file_holds_the_exact_export_line(self):
        result = run_hook(
            {
                "CLAUDE_ENV_FILE": str(self.env_file),
                "CLAUDE_PROJECT_DIR": str(self.root),
            }
        )
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(
            self.env_file.read_text(),
            f"export CLAUDE_PROJECT_DIR={self.root}\n",
        )

    def test_the_root_is_the_one_guard_resolves_from_the_event_cwd(self):
        result = run_hook(
            {"CLAUDE_ENV_FILE": str(self.env_file)}, {"cwd": str(self.root)}
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            self.env_file.read_text(),
            f"export CLAUDE_PROJECT_DIR={self.root}\n",
        )

    def test_fr_0025_397_a_line_written_by_another_hook_survives(self):
        self.env_file.write_text("export OTHER_HOOK=1\n")
        result = run_hook(
            {
                "CLAUDE_ENV_FILE": str(self.env_file),
                "CLAUDE_PROJECT_DIR": str(self.root),
            }
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            self.env_file.read_text(),
            f"export OTHER_HOOK=1\nexport CLAUDE_PROJECT_DIR={self.root}\n",
        )

    def test_a_predecessor_without_a_trailing_newline_is_not_fused(self):
        self.env_file.write_text("export OTHER=1")
        result = run_hook(
            {
                "CLAUDE_ENV_FILE": str(self.env_file),
                "CLAUDE_PROJECT_DIR": str(self.root),
            }
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            self.env_file.read_text(),
            f"export OTHER=1\nexport CLAUDE_PROJECT_DIR={self.root}\n",
        )

    def test_an_empty_file_gets_just_the_export_line(self):
        self.env_file.write_text("")
        result = run_hook(
            {
                "CLAUDE_ENV_FILE": str(self.env_file),
                "CLAUDE_PROJECT_DIR": str(self.root),
            }
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            self.env_file.read_text(),
            f"export CLAUDE_PROJECT_DIR={self.root}\n",
        )

    def test_the_cwd_fallthrough_is_not_exported(self):
        self.env_file.write_text("")
        for event in ({}, {"hook_event_name": "SessionStart"}):
            with self.subTest(event=event):
                result = run_hook({"CLAUDE_ENV_FILE": str(self.env_file)}, event)
                self.assertEqual((result.returncode, result.stdout), (0, ""))
                self.assertEqual(self.env_file.read_text(), "")
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)
                self.assertIn("nothing exported", result.stderr)

    def test_imp_0025_25_variable_absent_does_nothing_and_exits_zero(self):
        result = run_hook({"CLAUDE_PROJECT_DIR": str(self.root)})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(list(self.base.iterdir()), [self.root])

    def test_imp_0025_25_variable_empty_does_nothing_and_exits_zero(self):
        result = run_hook({"CLAUDE_ENV_FILE": "", "CLAUDE_PROJECT_DIR": str(self.root)})
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(list(self.base.iterdir()), [self.root])

    def test_imp_0025_25_unwritable_path_exits_zero_without_traceback(self):
        for unwritable in (self.base / "missing" / "env.sh", self.root):
            with self.subTest(path=str(unwritable)):
                result = run_hook(
                    {
                        "CLAUDE_ENV_FILE": str(unwritable),
                        "CLAUDE_PROJECT_DIR": str(self.root),
                    }
                )
                self.assertEqual((result.returncode, result.stdout), (0, ""))
                self.assertNotIn("Traceback", result.stderr)


class TestRegistration(unittest.TestCase):
    def test_hooks_json_runs_it_on_every_session_start(self):
        hooks = json.loads((_PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
        entries = [
            entry
            for entry in hooks["SessionStart"]
            if any(HOOK_NAME in h["command"] for h in entry["hooks"])
        ]
        self.assertEqual(len(entries), 1)
        self.assertNotIn("matcher", entries[0])


if __name__ == "__main__":
    unittest.main()
