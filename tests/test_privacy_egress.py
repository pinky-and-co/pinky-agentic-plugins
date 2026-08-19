#!/usr/bin/env python3
"""Tests for plugins/pc-core/hooks/privacy_egress.py — run with:
python3 -m unittest discover tests

Defines "done": a PreToolUse Read of a file whose CONTENT carries secret-shaped data returns
permissionDecision "deny" (before the content reaches the cloud model); a clean file or a
non-Read/non-Bash tool returns no decision; a line opting out with `# pragma: allowlist secret`
is not treated as a leak; a missing/unreadable file never raises and never blocks.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "hooks"))

import privacy_egress as pe

SECRET_LINE = (
    "api_key = sk-ABCD1234efgh5678ijkl"  # pragma: allowlist secret  gitleaks:allow
)


def _read_event(path: Path) -> dict:
    return {"tool_name": "Read", "tool_input": {"file_path": str(path)}}


def _bash_event(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


class TestDecideRead(unittest.TestCase):
    def test_denies_read_of_file_with_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_text(f"host = db\n{SECRET_LINE}\n", encoding="utf-8")
            decision = pe.decide(_read_event(f))
            self.assertIsNotNone(decision)
            self.assertEqual(
                decision["hookSpecificOutput"]["permissionDecision"], "deny"
            )

    def test_deny_reason_names_the_triggering_line_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_text(f"host = db\n{SECRET_LINE}\nport = 5432\n", encoding="utf-8")
            decision = pe.decide(_read_event(f))
            reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertIn("line 2", reason)
            self.assertIn("API-key prefix", reason)

    def test_allows_clean_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "code.py"
            f.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            self.assertIsNone(pe.decide(_read_event(f)))

    def test_ignores_non_read_non_bash_tool(self):
        self.assertIsNone(
            pe.decide({"tool_name": "Write", "tool_input": {"file_path": "x.py"}})
        )

    def test_allowlisted_line_is_not_a_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "test_fixture.py"
            f.write_text(
                f"x = {SECRET_LINE!r}  # pragma: allowlist secret\n", encoding="utf-8"
            )
            self.assertIsNone(pe.decide(_read_event(f)))

    def test_missing_file_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(pe.decide(_read_event(Path(tmp) / "nope.txt")))


class TestSecretLines(unittest.TestCase):
    def test_returns_line_numbers_with_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_text(
                f"clean\n{SECRET_LINE}\nclean\n{SECRET_LINE}\n", encoding="utf-8"
            )
            hits = pe.secret_lines(f)
            self.assertEqual([n for n, _ in hits], [2, 4])
            self.assertTrue(all(why for _, why in hits))


class TestVetting(unittest.TestCase):
    """tracked = vetted is not enough: the pre-commit gate plays at COMMIT time, so a staged or
    modified tracked file has passed no gate at all — only content identical to HEAD is exempt."""

    def _git_repo_with(self, tmp, name, content, *, track, commit=False):
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        f = tmp / name
        f.write_text(content, encoding="utf-8")
        if track:
            subprocess.run(["git", "add", "--", name], cwd=tmp, check=True)
        if commit:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=t@t",
                    "-c",
                    "user.name=t",
                    "commit",
                    "-q",
                    "-m",
                    "x",
                ],
                cwd=tmp,
                check=True,
            )
        return f

    def test_committed_clean_file_with_secret_is_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = self._git_repo_with(
                Path(tmp),
                "config.py",
                f"x = 1\n{SECRET_LINE}\n",
                track=True,
                commit=True,
            )
            self.assertTrue(pe.is_vetted(f))
            self.assertIsNone(pe.decide(_read_event(f)))

    def test_staged_but_never_committed_secret_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = self._git_repo_with(
                Path(tmp),
                "config.py",
                f"x = 1\n{SECRET_LINE}\n",
                track=True,
                commit=False,
            )
            self.assertFalse(pe.is_vetted(f))
            decision = pe.decide(_read_event(f))
            self.assertIsNotNone(decision)
            self.assertEqual(
                decision["hookSpecificOutput"]["permissionDecision"], "deny"
            )

    def test_committed_then_modified_with_secret_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = self._git_repo_with(
                Path(tmp), "config.py", "x = 1\n", track=True, commit=True
            )
            f.write_text(f"x = 1\n{SECRET_LINE}\n", encoding="utf-8")
            decision = pe.decide(_read_event(f))
            self.assertIsNotNone(decision)

    def test_untracked_file_with_secret_is_still_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = self._git_repo_with(
                Path(tmp), "dump.sql", f"-- dump\n{SECRET_LINE}\n", track=False
            )
            self.assertFalse(pe.is_vetted(f))
            decision = pe.decide(_read_event(f))
            self.assertIsNotNone(decision)


class TestDecideBash(unittest.TestCase):
    """The frictionless bypass channel: a Bash `cat`/`head`/… dumps content just like a Read."""

    def test_bash_cat_of_secret_file_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "dump.txt"
            f.write_text(f"host = db\n{SECRET_LINE}\n", encoding="utf-8")
            decision = pe.decide(_bash_event(f"cat '{f}'"))
            self.assertIsNotNone(decision)
            self.assertEqual(
                decision["hookSpecificOutput"]["permissionDecision"], "deny"
            )

    def test_bash_head_in_pipeline_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "dump.txt"
            f.write_text(f"{SECRET_LINE}\n", encoding="utf-8")
            self.assertIsNotNone(pe.decide(_bash_event(f"head -n 3 '{f}' | wc -l")))

    def test_bash_cat_of_clean_file_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "code.py"
            f.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            self.assertIsNone(pe.decide(_bash_event(f"cat '{f}'")))

    def test_bash_non_read_command_on_secret_file_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "dump.txt"
            f.write_text(f"{SECRET_LINE}\n", encoding="utf-8")
            self.assertIsNone(pe.decide(_bash_event(f"ls -la '{f}'")))


class TestMainEndToEnd(unittest.TestCase):
    """Real subprocess calls — same invocation shape Claude Code uses (JSON payload on stdin)."""

    SCRIPT = str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-core"
        / "hooks"
        / "privacy_egress.py"
    )

    def _run(self, payload: dict) -> subprocess.CompletedProcess:
        import json

        return subprocess.run(
            [sys.executable, self.SCRIPT],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_denies_over_stdin(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_text(SECRET_LINE, encoding="utf-8")
            result = self._run(_read_event(f))
            output = json.loads(result.stdout)
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_silent_over_stdin_for_clean_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "code.py"
            f.write_text("x = 1\n", encoding="utf-8")
            result = self._run(_read_event(f))
            self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
