#!/usr/bin/env python3
"""Tests for plugins/pc-core/hooks/inject_policies.py — run with:
python3 -m unittest discover tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "hooks"))

import inject_policies as ip


class TestDetectArtefactTypes(unittest.TestCase):
    def test_git_commit_bash(self):
        self.assertEqual(
            ip.detect_artefact_types("Bash", {"command": "git commit -m 'x'"}),
            ["commit"],
        )

    def test_gh_pr_create_bash(self):
        self.assertEqual(
            ip.detect_artefact_types("Bash", {"command": "gh pr create --title x"}),
            ["PR/MR"],
        )

    def test_glab_mr_create_bash(self):
        self.assertEqual(
            ip.detect_artefact_types("Bash", {"command": "glab mr create --title x"}),
            ["PR/MR"],
        )

    def test_irrelevant_bash_command(self):
        self.assertEqual(ip.detect_artefact_types("Bash", {"command": "ls -la"}), [])

    def test_write_python_file_returns_code_types(self):
        types = ip.detect_artefact_types("Write", {"file_path": "src/app.py"})
        self.assertEqual(
            set(types), {"code_identifiers", "docstrings", "inline_comments"}
        )

    def test_edit_docs_reference_returns_public_docs(self):
        self.assertEqual(
            ip.detect_artefact_types("Edit", {"file_path": "docs/reference/api.md"}),
            ["public_docs"],
        )

    def test_edit_adr_returns_explanation(self):
        self.assertEqual(
            ip.detect_artefact_types("Edit", {"file_path": "docs/adr/0001-x.md"}),
            ["explanation"],
        )

    def test_edit_unrelated_tool_returns_empty(self):
        self.assertEqual(
            ip.detect_artefact_types("Read", {"file_path": "src/app.py"}), []
        )


class TestWantsBranchWorkflow(unittest.TestCase):
    def test_gh_pr_create_wants_it(self):
        self.assertTrue(ip.wants_branch_workflow("Bash", {"command": "gh pr create"}))

    def test_git_push_dash_u_wants_it(self):
        self.assertTrue(
            ip.wants_branch_workflow(
                "Bash", {"command": "git push -u origin feature/x"}
            )
        )

    def test_plain_git_status_does_not_want_it(self):
        self.assertFalse(ip.wants_branch_workflow("Bash", {"command": "git status"}))

    def test_non_bash_tool_does_not_want_it(self):
        self.assertFalse(ip.wants_branch_workflow("Write", {"command": "gh pr create"}))


class TestResolveRepoRoot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolves_real_git_repo_root(self):
        nested = self.repo / "a" / "b"
        nested.mkdir(parents=True)
        resolved = ip.resolve_repo_root(str(self.repo), str(nested / "file.py"))
        self.assertEqual(resolved.resolve(), self.repo.resolve())

    def test_falls_back_to_project_dir_outside_any_repo(self):
        with tempfile.TemporaryDirectory() as outside:
            resolved = ip.resolve_repo_root(outside, None)
            self.assertEqual(resolved, Path(outside))


class TestLoadPolicies(unittest.TestCase):
    def test_loads_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude").mkdir()
            (root / ".claude" / "policies.json").write_text(
                '{"branch_workflow": "git flow"}'
            )
            self.assertEqual(ip.load_policies(root), {"branch_workflow": "git flow"})

    def test_returns_none_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ip.load_policies(Path(tmp)))

    def test_returns_none_on_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude").mkdir()
            (root / ".claude" / "policies.json").write_text("{not json")
            self.assertIsNone(ip.load_policies(root))


class TestReverseLanguageMap(unittest.TestCase):
    def test_inverts_language_to_artefact_types(self):
        policies = {
            "language": {
                "en": ["code_identifiers", "commit"],
                "fr": ["inline_comments"],
            }
        }
        self.assertEqual(
            ip.reverse_language_map(policies),
            {"code_identifiers": "en", "commit": "en", "inline_comments": "fr"},
        )

    def test_empty_policies_returns_empty_map(self):
        self.assertEqual(ip.reverse_language_map({}), {})


class TestMainEndToEnd(unittest.TestCase):
    """Real subprocess calls — same invocation shape Claude Code uses (JSON payload on stdin)."""

    SCRIPT = str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-core"
        / "hooks"
        / "inject_policies.py"
    )

    def _run(
        self, payload: dict, cwd: Path, project_dir: str
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, self.SCRIPT],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=cwd,
            env={
                "CLAUDE_PROJECT_DIR": project_dir,
                "PATH": "/usr/bin:/bin:/usr/local/bin",
            },
            check=False,
        )

    def test_silent_when_no_policies_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            result = self._run(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(root / "app.py")},
                },
                cwd=root,
                project_dir=str(root),
            )
            self.assertEqual(result.stdout.strip(), "")

    def test_injects_language_policy_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / ".claude").mkdir()
            (root / ".claude" / "policies.json").write_text(
                json.dumps(
                    {
                        "language": {
                            "en": ["code_identifiers", "docstrings", "inline_comments"]
                        }
                    }
                )
            )
            result = self._run(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(root / "app.py")},
                },
                cwd=root,
                project_dir=str(root),
            )
            output = json.loads(result.stdout)
            self.assertIn(
                "Language policy", output["hookSpecificOutput"]["additionalContext"]
            )


if __name__ == "__main__":
    unittest.main()
