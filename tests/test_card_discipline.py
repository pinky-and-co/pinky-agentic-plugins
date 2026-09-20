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
    """Build a `_KANBAN/CHANTIER0025-x/<column>/<name>` path under `tmp`, parents created."""
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


class TestWithinHandoffBlock(unittest.TestCase):
    def test_true_when_inside(self):
        text = f"# Card\n\n{cd.BLOCK_START}\nold state\n{cd.BLOCK_END}\n"
        self.assertTrue(cd.within_handoff_block(text, "old state"))

    def test_false_when_outside(self):
        text = f"# Card\n\nintro\n\n{cd.BLOCK_START}\nstate\n{cd.BLOCK_END}\n"
        self.assertFalse(cd.within_handoff_block(text, "intro"))

    def test_false_when_no_block(self):
        text = "# Card\n\nno block here\n"
        self.assertFalse(cd.within_handoff_block(text, "no block here"))

    def test_false_when_spans_outside_block(self):
        text = f"before\n{cd.BLOCK_START}\nstate\n{cd.BLOCK_END}\nafter"
        self.assertFalse(cd.within_handoff_block(text, "state\nafter"))


class TestDecide(unittest.TestCase):
    def test_ideas_asks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card_path(Path(tmp), "0-ideas")
            path.write_text("# idea\n")
            event = {
                "tool_name": "Write",
                "tool_input": {"file_path": str(path), "content": "x"},
            }
            decision = cd.decide(event)
            self.assertIsNotNone(decision)
            self.assertEqual(
                decision["hookSpecificOutput"]["permissionDecision"], "ask"
            )

    def test_todo_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card_path(Path(tmp), "1-todo")
            path.write_text("# todo\n")
            event = {
                "tool_name": "Write",
                "tool_input": {"file_path": str(path), "content": "x"},
            }
            self.assertIsNone(cd.decide(event))

    def test_selected_for_dev_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card_path(Path(tmp), "2-selected-for-dev")
            path.write_text("# selected\n")
            event = {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(path),
                    "old_string": "a",
                    "new_string": "b",
                },
            }
            self.assertIsNone(cd.decide(event))

    def test_in_progress_outside_markers_asks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card_path(Path(tmp), "3-in-progress")
            path.write_text(
                f"# card\n\nnarrative\n\n{cd.BLOCK_START}\nold\n{cd.BLOCK_END}\n"
            )
            event = {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(path),
                    "old_string": "narrative",
                    "new_string": "rewritten",
                },
            }
            decision = cd.decide(event)
            self.assertIsNotNone(decision)
            self.assertEqual(
                decision["hookSpecificOutput"]["permissionDecision"], "ask"
            )

    def test_in_progress_within_markers_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card_path(Path(tmp), "3-in-progress")
            path.write_text(f"# card\n\n{cd.BLOCK_START}\nold state\n{cd.BLOCK_END}\n")
            event = {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(path),
                    "old_string": "old state",
                    "new_string": "new state",
                },
            }
            self.assertIsNone(cd.decide(event))

    def test_in_progress_write_within_markers_still_asks(self):
        """A `Write` replaces the whole file — it can never be "between the markers"."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _card_path(Path(tmp), "3-in-progress")
            path.write_text(f"# card\n\n{cd.BLOCK_START}\nold state\n{cd.BLOCK_END}\n")
            event = {
                "tool_name": "Write",
                "tool_input": {"file_path": str(path), "content": "whatever"},
            }
            decision = cd.decide(event)
            self.assertIsNotNone(decision)
            self.assertEqual(
                decision["hookSpecificOutput"]["permissionDecision"], "ask"
            )

    def test_in_progress_multi_edit_all_within_markers_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card_path(Path(tmp), "3-in-progress")
            path.write_text(
                f"# card\n\n{cd.BLOCK_START}\nold state\nold branch\n{cd.BLOCK_END}\n"
            )
            event = {
                "tool_name": "MultiEdit",
                "tool_input": {
                    "file_path": str(path),
                    "edits": [
                        {"old_string": "old state", "new_string": "new state"},
                        {"old_string": "old branch", "new_string": "new branch"},
                    ],
                },
            }
            self.assertIsNone(cd.decide(event))

    def test_in_progress_multi_edit_one_entry_outside_markers_asks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card_path(Path(tmp), "3-in-progress")
            path.write_text(
                f"# card\n\nnarrative\n\n{cd.BLOCK_START}\nold state\n{cd.BLOCK_END}\n"
            )
            event = {
                "tool_name": "MultiEdit",
                "tool_input": {
                    "file_path": str(path),
                    "edits": [
                        {"old_string": "old state", "new_string": "new state"},
                        {"old_string": "narrative", "new_string": "rewritten"},
                    ],
                },
            }
            decision = cd.decide(event)
            self.assertIsNotNone(decision)
            self.assertEqual(
                decision["hookSpecificOutput"]["permissionDecision"], "ask"
            )

    def test_in_progress_notebook_edit_asks(self):
        """`NotebookEdit` carries no `old_string` at all — it can never qualify for the
        auto-handoff exception and always falls through to the column rule."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _card_path(Path(tmp), "3-in-progress")
            path.write_text(f"# card\n\n{cd.BLOCK_START}\nold state\n{cd.BLOCK_END}\n")
            event = {
                "tool_name": "NotebookEdit",
                "tool_input": {
                    "notebook_path": str(path),
                    "new_source": "print('hi')",
                },
            }
            decision = cd.decide(event)
            self.assertIsNotNone(decision)
            self.assertEqual(
                decision["hookSpecificOutput"]["permissionDecision"], "ask"
            )

    def test_done_asks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card_path(Path(tmp), "4-done")
            path.write_text("# done\n")
            event = {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(path),
                    "old_string": "a",
                    "new_string": "b",
                },
            }
            decision = cd.decide(event)
            self.assertIsNotNone(decision)
            self.assertEqual(
                decision["hookSpecificOutput"]["permissionDecision"], "ask"
            )

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
            path = _card_path(Path(tmp), "3-in-progress")
            path.write_text("# card\n")
            result = self._run(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(path), "content": "x"},
                }
            )
            output = json.loads(result.stdout)
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_silent_on_todo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _card_path(Path(tmp), "1-todo")
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
