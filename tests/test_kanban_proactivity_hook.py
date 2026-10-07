#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/hooks/kanban-proactivity/kanban_proactivity.py."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_PLUGIN = Path(__file__).parent.parent / "plugins" / "pc-kanban"
_DIR = _PLUGIN / "hooks" / "kanban-proactivity"
_SCRIPT = _DIR / "kanban_proactivity.py"
_RULE = _DIR / "kanban-proactivity.md"


def _run(script: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)],
        input="{}",
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestKanbanProactivityHook(unittest.TestCase):
    def test_output_is_whole_expected_json(self):
        proc = _run(_SCRIPT)
        expected = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": _RULE.read_text(encoding="utf-8"),
            }
        }
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(_RULE.read_text(encoding="utf-8").strip())
        self.assertEqual(json.loads(proc.stdout), expected)

    def test_output_is_deterministic_and_bounded(self):
        a, b = _run(_SCRIPT), _run(_SCRIPT)
        self.assertEqual(a.stdout, b.stdout)
        self.assertLess(len(a.stdout), 10_000)

    def test_rule_states_the_card_slug_limit(self):
        self.assertIn("at most 30 characters", _RULE.read_text(encoding="utf-8"))

    def test_rule_says_to_take_a_card_in_place_when_the_skill_is_loaded(self):
        self.assertIn("in place when already loaded", _RULE.read_text(encoding="utf-8"))

    def test_missing_rule_file_exits_zero_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / _SCRIPT.name
            shutil.copy(_SCRIPT, copy)
            proc = _run(copy)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_hooks_json_registers_session_start_matcher(self):
        hooks = json.loads((_PLUGIN / "hooks" / "hooks.json").read_text())
        entries = [
            e
            for e in hooks["hooks"]["SessionStart"]
            if e.get("matcher") == "startup|compact|clear"
        ]
        self.assertEqual(len(entries), 1)
        cmd = entries[0]["hooks"][0]["command"]
        self.assertIn("hooks/kanban-proactivity/kanban_proactivity.py", cmd)

    def test_orchestrator_freeze_entry_stays_unmatched(self):
        hooks = json.loads((_PLUGIN / "hooks" / "hooks.json").read_text())
        freeze = [
            e
            for e in hooks["hooks"]["SessionStart"]
            if "orchestrator_freeze.py" in e["hooks"][0]["command"]
        ]
        self.assertEqual(len(freeze), 1)
        self.assertNotIn("matcher", freeze[0])


if __name__ == "__main__":
    unittest.main()
