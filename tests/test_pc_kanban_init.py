#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/skills/init/scripts/pc_kanban_init.py — run with:
python3 -m unittest discover tests
"""

import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
import sys

sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-kanban"
        / "skills"
        / "init"
        / "scripts"
    ),
)

import pc_kanban_init as pki


class InitTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()
        self.settings = Path(self._tmp.name) / "settings.pinky.json"
        self._patch = unittest.mock.patch.object(
            pki, "SETTINGS_PINKY_PATH", self.settings
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)
        # A dev shell still carrying the retired KANBAN_ROOTS must not leak into a test that
        # asserts on its absence.
        os.environ.pop(pki._ROOTS_ENV, None)

    def _settings_data(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))


class TestRenderIndex(unittest.TestCase):
    def test_empty_board_is_header_only(self):
        self.assertEqual(
            pki.render_index([]),
            "---\n"
            "title: KANBAN\n"
            "---\n"
            "# _KANBAN — board de chantiers\n"
            "\n"
            "## Board\n"
            "\n"
            f"{pki.LEGEND}\n"
            "\n"
            "| # | Chantier (objectif) | Statut |\n"
            "|---|---------------------|--------|\n",
        )

    def test_recovered_chantiers_become_dormant_rows(self):
        self.assertEqual(
            pki.render_index([("0025", "plugins-claude"), ("0099", "demo")]),
            "---\n"
            "title: KANBAN\n"
            "---\n"
            "# _KANBAN — board de chantiers\n"
            "\n"
            "## Board\n"
            "\n"
            f"{pki.LEGEND}\n"
            "\n"
            "| # | Chantier (objectif) | Statut |\n"
            "|---|---------------------|--------|\n"
            "| 0025 | plugins-claude | ⚪ |\n"
            "| 0099 | demo | ⚪ |\n",
        )


class TestExistingChantiers(unittest.TestCase):
    def test_scans_chantier_dirs_ignoring_non_matching_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            kanban = Path(tmp)
            (kanban / "CHANTIER0099-demo").mkdir()
            (kanban / "CHANTIER0001-alpha").mkdir()
            (kanban / "INDEX.md").write_text("x\n")
            (kanban / "CHANTIERnope").mkdir()
            self.assertEqual(
                pki.existing_chantiers(kanban),
                [("0001", "alpha"), ("0099", "demo")],
            )

    def test_no_chantiers_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pki.existing_chantiers(Path(tmp)), [])


class TestLegacyDeclaredRoots(InitTestCase):
    def test_folders_of_the_nearest_code_workspace_are_migrated(self):
        (self.root / "x.code-workspace").write_text(
            json.dumps({"folders": [{"path": "."}, {"path": "../shared"}]}),
            encoding="utf-8",
        )
        shared = self.root.parent / "shared"
        (shared / "_KANBAN").mkdir(parents=True)

        self.assertEqual(pki.legacy_declared_roots(self.root), [shared.resolve()])

    def test_self_reference_is_excluded(self):
        (self.root / "x.code-workspace").write_text(
            json.dumps({"folders": [{"path": "."}]}), encoding="utf-8"
        )
        self.assertEqual(pki.legacy_declared_roots(self.root), [])

    def test_folder_with_no_kanban_dir_is_excluded(self):
        (self.root / "x.code-workspace").write_text(
            json.dumps({"folders": [{"path": "../unmounted"}]}), encoding="utf-8"
        )
        (self.root.parent / "unmounted").mkdir()
        self.assertEqual(pki.legacy_declared_roots(self.root), [])

    def test_env_wins_over_a_declared_code_workspace(self):
        (self.root / "x.code-workspace").write_text(
            json.dumps({"folders": [{"path": "../shared"}]}), encoding="utf-8"
        )
        shared = self.root.parent / "shared"
        (shared / "_KANBAN").mkdir(parents=True)
        other = self.root.parent / "other"
        (other / "_KANBAN").mkdir(parents=True)
        with unittest.mock.patch.dict(os.environ, {pki._ROOTS_ENV: str(other)}):
            self.assertEqual(pki.legacy_declared_roots(self.root), [other.resolve()])

    def test_no_code_workspace_found_returns_empty(self):
        self.assertEqual(pki.legacy_declared_roots(self.root), [])


class TestWriteKanbanBoards(InitTestCase):
    def test_creates_the_settings_file_and_key_on_a_virgin_root(self):
        summary = pki.write_kanban_boards(self.root)

        self.assertTrue(self.settings.is_file())
        data = self._settings_data()
        self.assertEqual(data["kanban_boards"], {str(self.root.resolve()): []})
        self.assertNotIn("model_pool", data)
        self.assertNotIn("keys", data)
        self.assertIn(str(self.root.resolve()), summary)

    def test_relative_vs_absolute_root_resolve_to_one_key(self):
        cwd = os.getcwd()
        try:
            os.chdir(self.root.parent)
            pki.write_kanban_boards(Path(self.root.name))
            pki.write_kanban_boards(self.root.resolve())
        finally:
            os.chdir(cwd)

        data = self._settings_data()
        self.assertEqual(list(data["kanban_boards"].keys()), [str(self.root.resolve())])

    def test_model_pool_and_keys_are_never_touched(self):
        self.settings.write_text(
            json.dumps({"model_pool": [{"name": "x"}], "keys": [{"env": "Y"}]}),
            encoding="utf-8",
        )

        pki.write_kanban_boards(self.root)

        data = self._settings_data()
        self.assertEqual(data["model_pool"], [{"name": "x"}])
        self.assertEqual(data["keys"], [{"env": "Y"}])

    def test_legacy_root_is_merged_in(self):
        (self.root / "x.code-workspace").write_text(
            json.dumps({"folders": [{"path": "."}, {"path": "../shared"}]}),
            encoding="utf-8",
        )
        shared = self.root.parent / "shared"
        (shared / "_KANBAN").mkdir(parents=True)

        pki.write_kanban_boards(self.root)

        data = self._settings_data()
        self.assertEqual(
            data["kanban_boards"][str(self.root.resolve())], [str(shared.resolve())]
        )

    def test_union_never_drops_an_already_declared_entry(self):
        other = Path(self._tmp.name) / "already-declared"
        other.mkdir()
        other = other.resolve()
        self.settings.write_text(
            json.dumps({"kanban_boards": {str(self.root.resolve()): [str(other)]}}),
            encoding="utf-8",
        )

        pki.write_kanban_boards(self.root)

        data = self._settings_data()
        self.assertEqual(data["kanban_boards"][str(self.root.resolve())], [str(other)])

    def test_rerun_with_nothing_new_does_not_touch_the_file(self):
        pki.write_kanban_boards(self.root)
        before = self.settings.read_text(encoding="utf-8")
        before_mtime = self.settings.stat().st_mtime_ns

        pki.write_kanban_boards(self.root)

        self.assertEqual(self.settings.read_text(encoding="utf-8"), before)
        self.assertEqual(self.settings.stat().st_mtime_ns, before_mtime)

    def test_root_is_never_declared_in_its_own_list(self):
        (self.root / "x.code-workspace").write_text(
            json.dumps({"folders": [{"path": "."}]}), encoding="utf-8"
        )
        (self.root / "_KANBAN").mkdir()

        pki.write_kanban_boards(self.root)

        data = self._settings_data()
        self.assertEqual(data["kanban_boards"][str(self.root.resolve())], [])

    def test_a_hand_added_tilde_entry_survives_a_rewrite_verbatim(self):
        """A rewrite is triggered by a genuinely new legacy entry; the pre-existing `~/...`
        string must come back unexpanded and un-re-anchored, not `Path.resolve()`d against
        whatever cwd this call happens to run from (the exact mangling this test guards)."""
        (self.root / "x.code-workspace").write_text(
            json.dumps({"folders": [{"path": "."}, {"path": "../shared"}]}),
            encoding="utf-8",
        )
        shared = self.root.parent / "shared"
        (shared / "_KANBAN").mkdir(parents=True)
        self.settings.write_text(
            json.dumps({"kanban_boards": {str(self.root.resolve()): ["~/hand-added"]}}),
            encoding="utf-8",
        )

        pki.write_kanban_boards(self.root)

        data = self._settings_data()
        entries = data["kanban_boards"][str(self.root.resolve())]
        self.assertIn("~/hand-added", entries)
        self.assertIn(str(shared.resolve()), entries)

    def test_malformed_settings_file_is_never_overwritten(self):
        self.settings.write_text("not json", encoding="utf-8")

        with self.assertRaises(SystemExit):
            pki.write_kanban_boards(self.root)

        self.assertEqual(self.settings.read_text(encoding="utf-8"), "not json")

    def test_settings_file_whose_top_level_is_a_list_is_never_overwritten(self):
        self.settings.write_text("[1, 2, 3]", encoding="utf-8")

        with self.assertRaises(SystemExit):
            pki.write_kanban_boards(self.root)

        self.assertEqual(self.settings.read_text(encoding="utf-8"), "[1, 2, 3]")

    def test_write_leaves_no_tmp_file_behind(self):
        pki.write_kanban_boards(self.root)
        self.assertEqual(
            list(self.settings.parent.glob(f"{self.settings.name}.tmp")), []
        )


class TestInit(InitTestCase):
    def test_creates_kanban_and_nested_agent_log_on_a_virgin_root(self):
        summary = pki.init(self.root)

        self.assertTrue((self.root / "_KANBAN").is_dir())
        self.assertTrue((self.root / "_KANBAN" / "_AGENT_LOG").is_dir())
        self.assertTrue((self.root / "_KANBAN" / "INDEX.md").is_file())
        self.assertIn(str(self.root / "_KANBAN"), summary)

    def test_never_scaffolds_a_first_chantier(self):
        pki.init(self.root)
        self.assertEqual(list((self.root / "_KANBAN").glob("CHANTIER*")), [])

    def test_index_recovers_chantiers_already_on_disk(self):
        (self.root / "_KANBAN" / "CHANTIER0025-plugins-claude").mkdir(parents=True)

        pki.init(self.root)

        text = (self.root / "_KANBAN" / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("| 0025 | plugins-claude | ⚪ |", text)

    def test_index_already_present_is_never_overwritten(self):
        kanban = self.root / "_KANBAN"
        kanban.mkdir()
        (kanban / "INDEX.md").write_text("hand-authored\n", encoding="utf-8")

        pki.init(self.root)

        self.assertEqual(
            (kanban / "INDEX.md").read_text(encoding="utf-8"), "hand-authored\n"
        )

    def test_pre_existing_sibling_agent_log_is_folded_in_not_shadowed(self):
        kanban = self.root / "_KANBAN"
        kanban.mkdir()
        sibling = self.root / "_AGENT_LOG"
        sibling.mkdir()
        (sibling / "entry.md").write_text("hello\n", encoding="utf-8")

        pki.init(self.root)

        nested = kanban / "_AGENT_LOG"
        self.assertTrue(nested.is_dir())
        self.assertEqual((nested / "entry.md").read_text(encoding="utf-8"), "hello\n")
        self.assertTrue(sibling.is_symlink())

    def test_rerun_on_an_already_scaffolded_root_changes_nothing(self):
        pki.init(self.root)
        index = self.root / "_KANBAN" / "INDEX.md"
        index_before = index.read_text(encoding="utf-8")
        index_mtime_before = index.stat().st_mtime_ns
        settings_before = self.settings.read_text(encoding="utf-8")

        pki.init(self.root)

        self.assertEqual(index.read_text(encoding="utf-8"), index_before)
        self.assertEqual(index.stat().st_mtime_ns, index_mtime_before)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), settings_before)

    def test_refuses_to_nest_a_second_board_under_an_already_boarded_ancestor(self):
        pki.init(self.root)
        nested_root = self.root / "sub" / "deeper"
        nested_root.mkdir(parents=True)

        with self.assertRaises(SystemExit):
            pki.init(nested_root)

        self.assertEqual(list((nested_root).glob("_KANBAN")), [])

    def test_migration_failure_is_never_papered_over_with_a_blank_agent_log(self):
        """Both the sibling and the nested `_AGENT_LOG/` already real (the migration script's own
        refusal case) must stop `init` cold — never silently create an empty nested dir next to
        content the migration couldn't fold in."""
        kanban = self.root / "_KANBAN"
        kanban.mkdir()
        sibling = self.root / "_AGENT_LOG"
        sibling.mkdir()
        (sibling / "sibling-entry.md").write_text("sibling\n", encoding="utf-8")
        nested = kanban / "_AGENT_LOG"
        nested.mkdir()
        (nested / "nested-entry.md").write_text("nested\n", encoding="utf-8")

        with self.assertRaises(SystemExit):
            pki.init(self.root)

        self.assertFalse((kanban / "INDEX.md").exists())
        self.assertEqual(
            (sibling / "sibling-entry.md").read_text(encoding="utf-8"), "sibling\n"
        )
        self.assertEqual(
            (nested / "nested-entry.md").read_text(encoding="utf-8"), "nested\n"
        )


if __name__ == "__main__":
    unittest.main()
