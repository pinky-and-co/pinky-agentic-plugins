#!/usr/bin/env python3
"""Tests for plugins/pc-secrets/hooks/privacy_egress.py — run with:
python3 -m unittest discover tests

Defines "done": a PreToolUse Read of a file whose CONTENT carries secret-shaped data returns
permissionDecision "deny" (before the content reaches the cloud model); a clean file or a
non-Read/non-Bash tool returns no decision; a line opting out with `# pragma: allowlist secret`
is not treated as a leak; a missing/unreadable file never raises and never blocks.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(
    0, str(Path(__file__).parent.parent / "plugins" / "pc-secrets" / "hooks")
)

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


class TestAgentOwnScope(unittest.TestCase):
    """The agent's own notes and config — the resolved config dir, any `.claude` folder — are
    never scanned: blocking the agent on its own journal is all friction, no protection. The
    criterion is the folder, not the access channel (Read and read-like Bash alike)."""

    def test_read_and_bash_cat_under_a_claude_component_yield_no_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "repo" / ".claude" / "settings.json"
            f.parent.mkdir(parents=True)
            f.write_text(f"{SECRET_LINE}\n", encoding="utf-8")
            self.assertIsNone(pe.decide(_read_event(f)))
            self.assertIsNone(pe.decide(_bash_event(f"cat '{f}'")))

    def test_read_and_bash_cat_under_the_config_dir_yield_no_decision(self):
        # Le silo mémoire par défaut vit sous le config dir — couvert mécaniquement.
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "silo" / "projects" / "demo" / "memory" / "note.md"
            note.parent.mkdir(parents=True)
            note.write_text(f"{SECRET_LINE}\n", encoding="utf-8")
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(Path(tmp) / "silo")}):
                self.assertIsNone(pe.decide(_read_event(note)))
                self.assertIsNone(pe.decide(_bash_event(f"cat '{note}'")))

    def test_a_plain_file_outside_the_excluded_scope_is_still_denied(self):
        # Contraste : même arbre, hors du périmètre exclu — le garde garde son comportement.
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_text(f"{SECRET_LINE}\n", encoding="utf-8")
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(Path(tmp) / "silo")}):
                decision = pe.decide(_read_event(f))
            self.assertIsNotNone(decision)
            self.assertEqual(
                decision["hookSpecificOutput"]["permissionDecision"], "deny"
            )


class TestVerdict(unittest.TestCase):
    """The structured verdict the band mod asks for — same judgement as `decide`, with line numbers."""

    def _verdict(self, path: Path, pool: bool = False) -> dict:
        with patch.object(pe, "pool_reachable", return_value=pool):
            return pe.verdict(_read_event(path))

    def test_flagged_file_lists_its_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_text(f"host = db\n{SECRET_LINE}\n", encoding="utf-8")
            self.assertEqual(
                self._verdict(f),
                {
                    "flagged": True,
                    "path": str(f),
                    "lines": [2],
                    "digest": _digest(f),
                    "pragma": True,
                    "offline": False,
                },
            )

    def test_offline_follows_the_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_text(SECRET_LINE, encoding="utf-8")
            self.assertTrue(self._verdict(f, pool=True)["offline"])

    def test_clean_file_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "code.py"
            f.write_text("x = 1\n", encoding="utf-8")
            self.assertEqual(self._verdict(f), {"flagged": False})

    def test_bash_is_not_offered_a_choice(self):
        self.assertEqual(pe.verdict(_bash_event("cat creds.txt")), {"flagged": False})

    def test_no_pragma_offered_for_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "real.txt"
            target.write_text(SECRET_LINE, encoding="utf-8")
            link = Path(tmp) / "notes.txt"
            link.symlink_to(target)
            self.assertFalse(self._verdict(link)["pragma"])

    def test_no_pragma_offered_where_a_hash_comment_would_break_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.json"
            f.write_text(f'{{"k": "{SECRET_LINE}"}}\n', encoding="utf-8")
            self.assertFalse(self._verdict(f)["pragma"])

    def test_excluded_scope_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / ".claude" / "creds.txt"
            f.parent.mkdir()
            f.write_text(SECRET_LINE, encoding="utf-8")
            self.assertEqual(self._verdict(f), {"flagged": False})


def _digest(path: Path) -> str:
    return pe.file_digest(path)


class TestAddPragma(unittest.TestCase):
    def test_marks_the_given_lines_and_keeps_the_rest_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_bytes(b"host = db\r\n" + SECRET_LINE.encode() + b"\r\nlast")
            self.assertEqual(pe.add_pragma(f, [2], _digest(f)), 1)
            self.assertEqual(
                f.read_bytes(),
                b"host = db\r\n"
                + SECRET_LINE.encode()
                + b"  # pragma: allowlist secret\r\nlast",
            )

    def test_the_marked_file_is_then_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_text(f"{SECRET_LINE}\n", encoding="utf-8")
            pe.add_pragma(f, [1], _digest(f))
            self.assertEqual(pe.secret_lines(f), [])

    def test_a_line_already_allowlisted_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_text(f"{SECRET_LINE}  {pe.PRAGMA}\n", encoding="utf-8")
            before = f.read_bytes()
            self.assertEqual(pe.add_pragma(f, [1], _digest(f)), 0)
            self.assertEqual(f.read_bytes(), before)

    def test_a_line_that_no_longer_carries_a_secret_is_left_alone(self):
        # le fichier a changé pendant la retenue : la ligne 2 n'est plus celle que la personne a vue
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_text("host = db\nplain = value\n", encoding="utf-8")
            before = f.read_bytes()
            self.assertEqual(pe.add_pragma(f, [2], _digest(f)), 0)
            self.assertEqual(f.read_bytes(), before)

    def test_refuses_when_the_file_changed_since_the_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_text(f"{SECRET_LINE}\n", encoding="utf-8")
            digest = _digest(f)
            f.write_text(
                "other = sk-ZZZZ9999yyyy8888xxxx\n", encoding="utf-8"
            )  # pragma: allowlist secret
            before = f.read_bytes()
            self.assertEqual(pe.add_pragma(f, [1], digest), 0)
            self.assertEqual(f.read_bytes(), before)

    def test_refuses_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "real.txt"
            target.write_text(f"{SECRET_LINE}\n", encoding="utf-8")
            link = Path(tmp) / "notes.txt"
            link.symlink_to(target)
            before = target.read_bytes()
            self.assertEqual(pe.add_pragma(link, [1], _digest(link)), 0)
            self.assertEqual(target.read_bytes(), before)

    def test_refuses_a_format_where_a_hash_would_corrupt_the_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("creds.ini", "app.js", "x.properties"):
                f = Path(tmp) / name
                f.write_text(f"{SECRET_LINE}\n", encoding="utf-8")
                before = f.read_bytes()
                self.assertEqual(pe.add_pragma(f, [1], _digest(f)), 0, name)
                self.assertEqual(f.read_bytes(), before)

    def test_refuses_a_line_continued_with_a_backslash(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.sh"
            f.write_text(f"{SECRET_LINE} \\\nmore\n", encoding="utf-8")
            before = f.read_bytes()
            self.assertEqual(pe.add_pragma(f, [1], _digest(f)), 0)
            self.assertEqual(f.read_bytes(), before)

    def test_a_form_feed_stays_after_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_bytes(SECRET_LINE.encode() + b"\x0cnext\n")
            self.assertEqual(pe.add_pragma(f, [1], _digest(f)), 1)
            self.assertEqual(
                f.read_bytes(),
                SECRET_LINE.encode() + b"  # pragma: allowlist secret\x0cnext\n",
            )

    def test_refuses_a_format_without_hash_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.json"
            f.write_text("{}\n", encoding="utf-8")
            self.assertEqual(pe.add_pragma(f, [1], _digest(f)), 0)
            self.assertEqual(f.read_text(encoding="utf-8"), "{}\n")

    def test_a_line_number_past_the_end_marks_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_text("a\n", encoding="utf-8")
            self.assertEqual(pe.add_pragma(f, [9], _digest(f)), 0)


class TestPoolReachable(unittest.TestCase):
    def test_false_without_a_pi_config(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"HOME": tmp}),
        ):
            self.assertFalse(pe.pool_reachable())

    def test_false_when_the_server_does_not_answer(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"HOME": tmp}),
        ):
            cfg = Path(tmp) / ".pi" / "agent" / "models.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(
                '{"providers": {"mlx": {"baseUrl": "http://127.0.0.1:1/v1"}}}',
                encoding="utf-8",
            )
            self.assertFalse(pe.pool_reachable())


class TestPoolReachableHosts(unittest.TestCase):
    def _config(self, tmp: str, base: str) -> None:
        cfg = Path(tmp) / ".pi" / "agent" / "models.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(
            json.dumps({"providers": {"mlx": {"baseUrl": base}}}), encoding="utf-8"
        )

    def test_a_remote_server_is_not_the_local_pool(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"HOME": tmp}),
        ):
            self._config(tmp, "https://example.com/v1")
            with patch.object(pe.urllib.request, "urlopen") as opened:
                self.assertFalse(pe.pool_reachable())
            opened.assert_not_called()

    def test_a_local_server_answering_with_an_http_error_is_reachable(self):
        err = pe.urllib.error.HTTPError(
            "http://localhost:1/v1/models", 401, "no", {}, None
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"HOME": tmp}),
        ):
            self._config(tmp, "http://localhost:11234/v1")
            with patch.object(pe.urllib.request, "urlopen", side_effect=err):
                self.assertTrue(pe.pool_reachable())


class TestCli(unittest.TestCase):
    SCRIPT = str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-secrets"
        / "hooks"
        / "privacy_egress.py"
    )

    def test_verdict_over_stdin(self):
        import json

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_text(SECRET_LINE, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, self.SCRIPT, "--verdict"],
                input=json.dumps(_read_event(f)),
                capture_output=True,
                text=True,
                check=False,
            )
            out = json.loads(result.stdout)
            self.assertEqual((out["flagged"], out["lines"]), (True, [1]))

    def test_pragma_marks_the_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "creds.txt"
            f.write_text(SECRET_LINE, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, self.SCRIPT, "--pragma", str(f), _digest(f), "1"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual((result.returncode, result.stdout.strip()), (0, "1"))
            self.assertEqual(pe.secret_lines(f), [])


class TestMainEndToEnd(unittest.TestCase):
    """Real subprocess calls — same invocation shape Claude Code uses (JSON payload on stdin)."""

    SCRIPT = str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-secrets"
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
