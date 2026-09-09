#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/hooks/stop_handoff.py — run with:
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

sys.path.insert(
    0, str(Path(__file__).parent.parent / "plugins" / "pc-kanban" / "hooks")
)

import stop_handoff as sh


class TestParseFrontmatter(unittest.TestCase):
    def test_flat_scalars(self):
        text = "---\nstatus: in-progress\nmodel: sonnet\nsert: null\n---\nbody\n"
        self.assertEqual(
            sh.parse_frontmatter(text),
            {"status": "in-progress", "model": "sonnet", "sert": None},
        )

    def test_list_value(self):
        text = "---\nrepos: [pinky-bootstrap, pinky-agentic-plugins]\n---\n"
        self.assertEqual(
            sh.parse_frontmatter(text)["repos"],
            ["pinky-bootstrap", "pinky-agentic-plugins"],
        )

    def test_empty_list(self):
        text = "---\nrepos: []\n---\n"
        self.assertEqual(sh.parse_frontmatter(text)["repos"], [])

    def test_no_frontmatter_returns_none(self):
        self.assertIsNone(
            sh.parse_frontmatter("# just a heading\n\nno frontmatter here")
        )

    def test_comment_and_blank_lines_skipped(self):
        text = "---\n# a comment\n\nstatus: todo\n---\n"
        self.assertEqual(sh.parse_frontmatter(text), {"status": "todo"})


class TestCardRepos(unittest.TestCase):
    def test_yaml_repos(self):
        text = "---\nrepos: [pinky-bootstrap]\n---\n"
        self.assertEqual(sh.card_repos(text), ["pinky-bootstrap"])

    def test_yaml_present_without_repos_key_falls_through_to_legacy(self):
        text = "---\nstatus: todo\n---\n**Repos:** pinky-bootstrap\n"
        self.assertEqual(sh.card_repos(text), ["pinky-bootstrap"])

    def test_legacy_bold_line_no_frontmatter(self):
        text = "# Card\n\n**Repos:** pinky-bootstrap, pinky-agentic-plugins\n"
        self.assertEqual(
            sh.card_repos(text), ["pinky-agentic-plugins", "pinky-bootstrap"]
        )

    def test_neither_declared_returns_none(self):
        self.assertIsNone(sh.card_repos("# Card\n\nno scope declared\n"))


class TestCardAccepts(unittest.TestCase):
    def test_yaml_scope_matches_slug_not_bare_name(self):
        text = "---\nrepos: [pinky-and-co/pinky-bootstrap]\n---\n"
        self.assertTrue(
            sh.card_accepts(text, "pinky-bootstrap", "pinky-and-co/pinky-bootstrap")
        )
        # bare name alone never matches a YAML-format card — slug is required
        self.assertFalse(sh.card_accepts(text, "pinky-bootstrap", None))
        self.assertFalse(
            sh.card_accepts(text, "pinky-bootstrap", "other-org/pinky-bootstrap")
        )

    def test_legacy_scope_matches_bare_name(self):
        text = "**Repos:** pinky-bootstrap\n"
        self.assertTrue(
            sh.card_accepts(text, "pinky-bootstrap", "pinky-and-co/pinky-bootstrap")
        )
        self.assertFalse(
            sh.card_accepts(text, "other-repo", "pinky-and-co/pinky-bootstrap")
        )

    def test_falls_back_to_existing_block_repo_either_identity(self):
        text = f"{sh.BLOCK_START}\nRepo / branch: `pinky-bootstrap` @ `main`\n{sh.BLOCK_END}\n"
        self.assertTrue(sh.card_accepts(text, "pinky-bootstrap", None))
        slug_text = f"{sh.BLOCK_START}\nRepo / branch: `pinky-and-co/pinky-bootstrap` @ `main`\n{sh.BLOCK_END}\n"
        self.assertTrue(
            sh.card_accepts(slug_text, "other-name", "pinky-and-co/pinky-bootstrap")
        )
        self.assertFalse(sh.card_accepts(text, "other-repo", None))

    def test_no_scope_declared_never_accepts(self):
        self.assertFalse(
            sh.card_accepts("# Card\n\nnothing declared\n", "any-repo", "any/slug")
        )


class TestBuildBlock(unittest.TestCase):
    def test_exact_output(self):
        ctx = {
            "repo": "pinky-bootstrap",
            "branch": "main",
            "changed": ["a.py", "b.py"],
            "recent": ["fix: x", "feat: y"],
            "landmark": True,
        }
        self.assertEqual(
            sh.build_block(ctx),
            "\n".join(
                [
                    sh.BLOCK_START,
                    "## Handoff (auto)",
                    "- Repo / branch: `pinky-bootstrap` @ `main`  ⚑ develop→main landmark",
                    "- Uncommitted (2): a.py, b.py",
                    "- Recent commits: fix: x · feat: y",
                    f"- _Auto block — {sh.ANTI_SECRETS}. Narrative handoff above is the agent's job._",
                    sh.BLOCK_END,
                ]
            ),
        )

    def test_truncates_beyond_max_files(self):
        ctx = {
            "repo": "r",
            "branch": "b",
            "changed": [f"f{i}.py" for i in range(sh.MAX_FILES + 3)],
            "recent": [],
            "landmark": False,
        }
        block = sh.build_block(ctx)
        self.assertIn("(+3)", block)


class TestUpsertBlock(unittest.TestCase):
    def test_appends_when_absent(self):
        result = sh.upsert_block("# Card\n\nbody\n", "BLOCKCONTENT")
        self.assertTrue(result.endswith("BLOCKCONTENT\n"))
        self.assertIn("body", result)

    def test_replaces_existing_block_idempotently(self):
        original = f"# Card\n\n{sh.BLOCK_START}\nold\n{sh.BLOCK_END}\n"
        once = sh.upsert_block(original, f"{sh.BLOCK_START}\nnew\n{sh.BLOCK_END}")
        twice = sh.upsert_block(once, f"{sh.BLOCK_START}\nnew\n{sh.BLOCK_END}")
        self.assertEqual(once, twice)
        self.assertIn("new", once)
        self.assertNotIn("old", once)


class TestParseChanged(unittest.TestCase):
    def test_extracts_paths(self):
        status = " M src/app.py\n?? new_file.py\n"
        self.assertEqual(sh.parse_changed(status), ["src/app.py", "new_file.py"])

    def test_rename_reduces_to_new_path(self):
        status = "R  old.py -> new.py\n"
        self.assertEqual(sh.parse_changed(status), ["old.py -> new.py"])


class TestGitContext(unittest.TestCase):
    def _init_repo(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)

    def test_clean_tree_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            (root / "a.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
            self.assertIsNone(sh.git_context(root))

    def test_dirty_tree_returns_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            (root / "a.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
            (root / "a.py").write_text("x = 2\n")
            ctx = sh.git_context(root)
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx["repo"], root.name)
            self.assertIn("a.py", ctx["changed"])

    def test_not_a_git_repo_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(sh.git_context(Path(tmp)))


class TestRefreshCards(unittest.TestCase):
    def test_writes_only_matching_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matching = root / "match.md"
            other = root / "other.md"
            matching.write_text("---\nrepos: [org/pinky-bootstrap]\n---\n# Card\n")
            other.write_text("---\nrepos: [org/pinky-agentic-plugins]\n---\n# Card\n")
            ctx = {
                "repo": "org/pinky-bootstrap",
                "repo_name": "pinky-bootstrap",
                "repo_slug": "org/pinky-bootstrap",
                "branch": "main",
                "changed": ["a.py"],
                "recent": [],
                "landmark": False,
            }
            sh.refresh_cards([matching, other], ctx)
            self.assertIn(sh.BLOCK_START, matching.read_text())
            self.assertNotIn(sh.BLOCK_START, other.read_text())

    def test_idempotent_second_call_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            card = Path(tmp) / "card.md"
            card.write_text("---\nrepos: [org/r]\n---\n# Card\n")
            ctx = {
                "repo": "org/r",
                "repo_name": "r",
                "repo_slug": "org/r",
                "branch": "main",
                "changed": [],
                "recent": [],
                "landmark": False,
            }
            sh.refresh_cards([card], ctx)
            first = card.read_text()
            sh.refresh_cards([card], ctx)
            self.assertEqual(first, card.read_text())


class TestRemoteSlug(unittest.TestCase):
    def _repo_with_remote(self, root: Path, url: str) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "remote", "add", "origin", url], cwd=root, check=True)

    def test_https_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo_with_remote(
                root, "https://github.com/pinky-and-co/pinky-bootstrap.git"
            )
            self.assertEqual(sh.remote_slug(root), "pinky-and-co/pinky-bootstrap")

    def test_ssh_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo_with_remote(
                root, "git@github.com:pinky-and-co/pinky-bootstrap.git"
            )
            self.assertEqual(sh.remote_slug(root), "pinky-and-co/pinky-bootstrap")

    def test_no_remote_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            self.assertIsNone(sh.remote_slug(root))


class TestSecretScrubImport(unittest.TestCase):
    def test_scrub_is_imported_from_secret_patterns_not_redefined(self):
        # stop_handoff.py must not carry its own copy — full coverage of the patterns
        # themselves lives in test_secret_patterns.py.
        import pc_kanban_secret_patterns

        self.assertIs(sh.scrub, pc_kanban_secret_patterns.scrub)


class TestDeclaredKanbans(unittest.TestCase):
    """Boards declared via `folders[]` of the nearest `*.code-workspace`."""

    def test_folders_of_the_nearest_code_workspace_are_declared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pinky.code-workspace").write_text(
                json.dumps({"folders": [{"path": "."}, {"path": "../shared"}]}),
                encoding="utf-8",
            )
            deep = root / "pinky-core" / "src"
            deep.mkdir(parents=True)

            self.assertEqual(
                sh.declared_kanbans(deep),
                [
                    root.resolve() / "_KANBAN",
                    (root / "../shared").resolve() / "_KANBAN",
                ],
            )

    def test_absolute_folder_path_used_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            shared = Path(tmp) / "elsewhere" / "shared"
            shared.mkdir(parents=True)
            (root / "x.code-workspace").write_text(
                json.dumps({"folders": [{"path": str(shared)}]}), encoding="utf-8"
            )

            self.assertEqual(sh.declared_kanbans(root), [shared / "_KANBAN"])

    def test_real_code_workspace_files_are_jsonc_not_strict_json(self):
        # A real *.code-workspace carries `//` banner comments (path warnings, mount rationale) —
        # json.loads alone would raise, get caught, and silently return []. Found live 2026-08-20
        # against the author's own pinky.code-workspace: 0 boards instead of 2.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "shared"
            shared.mkdir()
            (root / "x.code-workspace").write_text(
                "// a warning about paths: not http://example.test either\n"
                "/* block\n   comment */\n"
                "{\n"
                '  "folders": [\n'
                '    { "name": "root", "path": "." }, // trailing note\n'
                f'    {{ "name": "shared", "path": {json.dumps(str(shared))} }}\n'
                "  ]\n"
                "}\n",
                encoding="utf-8",
            )

            self.assertEqual(
                sh.declared_kanbans(root),
                [root.resolve() / "_KANBAN", shared / "_KANBAN"],
            )

    def test_a_slash_slash_inside_a_string_is_not_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.code-workspace").write_text(
                '{"folders": [{"name": "http://example.test", "path": "."}]}',
                encoding="utf-8",
            )

            self.assertEqual(sh.declared_kanbans(root), [root.resolve() / "_KANBAN"])

    def test_no_code_workspace_found_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sh.declared_kanbans(Path(tmp)), [])

    def test_env_wins_over_a_declared_code_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.code-workspace").write_text(
                json.dumps({"folders": [{"path": "."}]}), encoding="utf-8"
            )
            other = root / "other"
            other.mkdir()
            with unittest.mock.patch.dict(os.environ, {sh.ROOTS_ENV: str(other)}):
                self.assertEqual(sh.declared_kanbans(root), [other / "_KANBAN"])


class TestUnreviewedDoneCards(unittest.TestCase):
    def _done(
        self, kanban: Path, chantier: str, name: str, body: str, date: str = ""
    ) -> Path:
        col = kanban / f"CHANTIER{chantier}" / "4-done"
        col.mkdir(parents=True, exist_ok=True)
        card = col / name
        fm = f"status: done\ndate: {date}\n" if date else "status: done\n"
        card.write_text(f"---\n{fm}---\n# {name}\n{body}\n", encoding="utf-8")
        return card

    def test_flags_only_the_unreviewed_by_date_desc_with_tags_and_mechanical_count(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            kb = Path(tmp) / "_KANBAN"
            self._done(
                kb, "0001", "checked.md", "- [x] reviewed (adversarial)", "2026-01-01"
            )
            self._done(
                kb,
                "0001",
                "mechanical.md",
                "- [x] reviewed (n/a — mécanique)",
                "2026-01-02",
            )
            unchecked = self._done(
                kb, "0002", "unchecked.md", "- [ ] reviewed (adversarial)", "2026-01-03"
            )
            silent = self._done(
                kb, "0002", "silent.md", "no review line here", "2026-01-04"
            )

            self.assertEqual(
                sh.unreviewed_done_cards(kb),
                (
                    [
                        (silent, "aucune ligne review"),
                        (unchecked, "reviewed non coché"),
                    ],
                    1,
                ),
            )

    def test_checked_line_matched_despite_spacing_case_and_colon_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = Path(tmp) / "_KANBAN"
            self._done(kb, "0001", "spaced.md", "  - [X]  reviewed  (reviewer)")
            self._done(kb, "0001", "colon.md", "- [x] reviewed: adversarial")
            self.assertEqual(sh.unreviewed_done_cards(kb), ([], 0))


class TestBoundedBoardLines(unittest.TestCase):
    def _board(self, tmp: str, index_body: str) -> Path:
        kb = Path(tmp) / "_KANBAN"
        kb.mkdir(parents=True, exist_ok=True)
        (kb / "INDEX.md").write_text(index_body, encoding="utf-8")
        return kb

    def _chantier(self, kb: Path, chantier: str, **counts: int) -> None:
        for col, n in counts.items():
            d = kb / f"CHANTIER{chantier}-slug" / col
            d.mkdir(parents=True, exist_ok=True)
            for i in range(n):
                (d / f"card{i}.md").write_text("x", encoding="utf-8")

    def test_drops_preamble_keeps_table_and_caps_long_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            long_status = "x" * 210
            index = (
                "# Preamble\n\nOnboarding prose, a topology diagram, a convention section.\n\n"
                "## Board\n\n"
                "| # | Chantier (objectif) | Statut |\n"
                "|---|---------------------|--------|\n"
                f"| 0001 | Un chantier | {long_status} |\n"
            )
            kb = self._board(tmp, index)
            self._chantier(
                kb,
                "0001",
                **{
                    "0-ideas": 1,
                    "1-todo": 0,
                    "2-selected-for-dev": 0,
                    "3-in-progress": 0,
                    "4-done": 2,
                },
            )

            lines = sh.bounded_board_lines(kb)

            self.assertEqual(
                lines,
                [
                    "| # | Chantier (objectif) | Statut |",
                    "|---|---------------------|--------|",
                    f"| 0001 | Un chantier | {'x' * 200}… (+10 car.) |",
                    "  cards: 1/0/0/0/2",
                ],
            )
            self.assertNotIn("Preamble", "\n".join(lines))

    def test_short_status_untouched_and_unmatched_chantier_counts_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = (
                "## Board\n\n"
                "| # | Chantier (objectif) | Statut |\n"
                "|---|---------------------|--------|\n"
                "| 0002 | Autre chantier | ⚪ |\n"
            )
            kb = self._board(tmp, index)

            lines = sh.bounded_board_lines(kb)

            self.assertEqual(
                lines,
                [
                    "| # | Chantier (objectif) | Statut |",
                    "|---|---------------------|--------|",
                    "| 0002 | Autre chantier | ⚪ |",
                    "  cards: 0/0/0/0/0",
                ],
            )

    def test_missing_index_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = Path(tmp) / "_KANBAN"
            kb.mkdir()
            self.assertEqual(sh.bounded_board_lines(kb), [])

    def test_escaped_pipe_in_status_cell_does_not_shift_columns(self):
        # A `\|` inside a code span (GFM's own escaping convention for a literal pipe in a table
        # cell) must not count as a column delimiter — splitting on it shifts every cell after it,
        # so a naive cap targets the wrong field instead of the real Statut tail. The tail is kept
        # long enough (> MAX_STATUS_CHARS) to force the capping path — a short cell hides this bug
        # entirely, since an uncapped row is returned verbatim regardless of how it was split.
        with tempfile.TemporaryDirectory() as tmp:
            content = "🟢 actif — `pinky docs serve\\|status\\|uninstall`. " + "x" * 220
            index = (
                "## Board\n\n"
                "| # | Chantier (objectif) | Statut |\n"
                "|---|---------------------|--------|\n"
                f"| 0027 | Live-docs | {content} |\n"
            )
            kb = self._board(tmp, index)

            lines = sh.bounded_board_lines(kb)

            # content is 269 chars: capped to the first 200 + the omitted-count marker, computed
            # independently here rather than re-deriving it from the function under test.
            omitted = len(content) - 200
            self.assertEqual(
                lines,
                [
                    "| # | Chantier (objectif) | Statut |",
                    "|---|---------------------|--------|",
                    f"| 0027 | Live-docs | {content[:200]}… (+{omitted} car.) |",
                    "  cards: 0/0/0/0/0",
                ],
            )

    def test_no_board_table_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = self._board(tmp, "# Just a preamble, no table at all.\n")
            self.assertEqual(sh.bounded_board_lines(kb), [])


if __name__ == "__main__":
    unittest.main()
