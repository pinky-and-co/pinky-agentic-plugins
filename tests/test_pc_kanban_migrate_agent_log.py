#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/skills/kanban/scripts/pc_kanban_migrate_agent_log.py — run with:
python3 -m unittest discover tests
"""

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-kanban"
        / "skills"
        / "kanban"
        / "scripts"
    ),
)

import pc_kanban_migrate_agent_log as mal


class TestMigrate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.kanban = self.root / "_KANBAN"
        self.kanban.mkdir()
        self.old = self.root / "_AGENT_LOG"
        self.new = self.kanban / "_AGENT_LOG"

    def test_moves_every_file_with_identical_content_and_removes_the_sibling(self):
        self.old.mkdir()
        (self.old / "2026-09-08_claude-code-Perrine_x.md").write_text("hello\n")
        (self.old / "2026-09-09_claude-code-Perrine_y.md").write_text("world\n")

        summary = mal.migrate(self.kanban)

        self.assertIn("migrated", summary)
        self.assertTrue(self.new.is_dir())
        self.assertEqual(
            (self.new / "2026-09-08_claude-code-Perrine_x.md").read_text(), "hello\n"
        )
        self.assertEqual(
            (self.new / "2026-09-09_claude-code-Perrine_y.md").read_text(), "world\n"
        )
        self.assertFalse(self.old.exists())

    def test_write_through_old_path_after_migration_creates_a_disjoint_dir(self):
        """No compat is kept — a write through the old path after migration lands in a fresh,
        disjoint directory, never the nested one. Accepted tradeoff, not a bug: coordinating
        every writer's plugin-cache refresh is out of this script's reach."""
        self.old.mkdir()
        (self.old / "note.md").write_text("x\n")
        mal.migrate(self.kanban)

        self.old.mkdir()
        (self.old / "fresh.md").write_text("new entry\n")

        self.assertFalse((self.new / "fresh.md").exists())

    def test_rerun_after_migration_is_a_noop(self):
        self.old.mkdir()
        (self.old / "note.md").write_text("x\n")
        mal.migrate(self.kanban)

        summary = mal.migrate(self.kanban)

        self.assertIn("already migrated", summary)
        self.assertEqual((self.new / "note.md").read_text(), "x\n")

    def test_fresh_board_with_no_sibling_is_a_noop(self):
        summary = mal.migrate(self.kanban)
        self.assertIn("nothing to migrate", summary)
        self.assertFalse(self.new.exists())
        self.assertFalse(self.old.exists())

    def test_both_real_directories_present_is_refused(self):
        self.old.mkdir()
        self.new.mkdir()
        with self.assertRaises(SystemExit) as cm:
            mal.migrate(self.kanban)
        self.assertIn("both", str(cm.exception))

    def test_existing_symlink_at_old_path_is_left_untouched(self):
        """A leftover compat symlink from a board migrated before compat was dropped — this
        script no longer creates one, and no longer manages one it finds either."""
        other = self.root / "elsewhere"
        other.mkdir()
        self.old.symlink_to(other, target_is_directory=True)

        summary = mal.migrate(self.kanban)

        self.assertIn("leftover", summary)
        self.assertTrue(self.old.is_symlink())
        self.assertEqual(self.old.resolve(), other.resolve())

    def test_new_as_a_stray_file_is_refused_not_a_crash(self):
        self.old.mkdir()
        self.new.write_text("not a directory\n")
        with self.assertRaises(SystemExit) as cm:
            mal.migrate(self.kanban)
        self.assertIn("not a directory", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
