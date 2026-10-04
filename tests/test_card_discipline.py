#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/hooks/card-discipline/card_discipline.py — run with:
python3 -m unittest discover tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-kanban"
        / "hooks"
        / "card-discipline"
    ),
)

import card_discipline as cd


def _card_path(tmp: Path, column: str, name: str = "my-card.md") -> Path:
    """Build a `_KANBAN/<chantier>/<column>/<name>` path under `tmp`, parents created."""
    path = tmp / "_KANBAN" / "CHANTIER0025-plugins-claude" / column / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class TestCardColumn(unittest.TestCase):
    def test_recognizes_each_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            for column in (
                "0-ideas",
                "1-todo",
                "2-selected-for-dev",
                "3-in-progress",
                "4-done",
            ):
                path = _card_path(Path(tmp), column)
                self.assertEqual(cd.card_column(str(path)), column)

    def test_none_outside_kanban(self):
        self.assertIsNone(cd.card_column("/some/repo/README.md"))

    def test_none_for_non_card_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "_KANBAN" / "INDEX.md"
            path.parent.mkdir(parents=True)
            self.assertIsNone(cd.card_column(str(path)))


def _write_event(path: Path) -> dict:
    """A `Write` of `path` — the shape both a creation and a full rewrite take."""
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(path), "content": "x"},
    }


def _edit_event(path: Path) -> dict:
    """An `Edit` of `path` replacing `a` with `b`."""
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path), "old_string": "a", "new_string": "b"},
    }


class TestDecide(unittest.TestCase):
    def test_ask_decision_is_the_full_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card_path(Path(tmp), "1-todo")
            path.write_text("# todo\n")
            self.assertEqual(
                cd.decide(_edit_event(path)),
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": cd._REASON,
                    }
                },
            )

    def test_framing_columns_ask_on_edit(self):
        for column in ("0-ideas", "1-todo", "2-selected-for-dev"):
            with self.subTest(column=column), tempfile.TemporaryDirectory() as tmp:
                path = _card_path(Path(tmp), column)
                path.write_text("# card\n")
                decision = cd.decide(_edit_event(path))
                self.assertIsNotNone(decision)
                self.assertEqual(
                    decision["hookSpecificOutput"]["permissionDecision"], "ask"
                )

    def test_framing_columns_ask_on_card_creation(self):
        """Creating a card is gated too — the file does not exist yet."""
        for column in ("0-ideas", "1-todo", "2-selected-for-dev"):
            with self.subTest(column=column), tempfile.TemporaryDirectory() as tmp:
                path = _card_path(Path(tmp), column)
                decision = cd.decide(_write_event(path))
                self.assertIsNotNone(decision)
                self.assertEqual(
                    decision["hookSpecificOutput"]["permissionDecision"], "ask"
                )

    def test_engaged_columns_pass_every_guarded_tool(self):
        for column in ("3-in-progress", "4-done"):
            with self.subTest(column=column), tempfile.TemporaryDirectory() as tmp:
                path = _card_path(Path(tmp), column)
                path.write_text("# card\n\nnarrative\n")
                events = [
                    _edit_event(path),
                    _write_event(path),
                    {
                        "tool_name": "MultiEdit",
                        "tool_input": {
                            "file_path": str(path),
                            "edits": [{"old_string": "narrative", "new_string": "x"}],
                        },
                    },
                    {
                        "tool_name": "NotebookEdit",
                        "tool_input": {"notebook_path": str(path), "new_source": "x"},
                    },
                ]
                for event in events:
                    self.assertIsNone(cd.decide(event), event["tool_name"])

    def test_ignores_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card_path(Path(tmp), "3-in-progress")
            path.write_text("# card\n")
            event = {"tool_name": "Read", "tool_input": {"file_path": str(path)}}
            self.assertIsNone(cd.decide(event))

    def test_no_decision_outside_kanban(self):
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/some/repo/README.md", "content": "x"},
        }
        self.assertIsNone(cd.decide(event))

    def test_no_decision_for_unrecognized_kanban_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "_KANBAN" / "INDEX.md"
            path.parent.mkdir(parents=True)
            path.write_text("# board\n")
            event = {
                "tool_name": "Write",
                "tool_input": {"file_path": str(path), "content": "x"},
            }
            self.assertIsNone(cd.decide(event))


class TestMainEndToEnd(unittest.TestCase):
    """Real subprocess calls — same invocation shape Claude Code uses (JSON payload on stdin)."""

    SCRIPT = str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-kanban"
        / "hooks"
        / "card-discipline"
        / "card_discipline.py"
    )

    def _run(self, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, self.SCRIPT],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_asks_over_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card_path(Path(tmp), "1-todo")
            path.write_text("# card\n")
            result = self._run(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(path), "content": "x"},
                }
            )
            output = json.loads(result.stdout)
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_silent_on_in_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card_path(Path(tmp), "3-in-progress")
            path.write_text("# card\n")
            result = self._run(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(path), "content": "x"},
                }
            )
            self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
