#!/usr/bin/env python3
"""Tests for plugins/pc-core/hooks/load_secrets.py — run with:
python3 -m unittest discover tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "hooks"))

import load_secrets as ls

_real_subprocess_run = subprocess.run


def _fake_security_only(cmd, *args, **kwargs):
    """Only mock the `security` call — `resolve_repo_root`'s own `git rev-parse` must stay real,
    or a global `subprocess.run` mock would hijack it too and resolve a garbage repo root."""
    if cmd[0] == "security":
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="sk-real\n", stderr=""
        )
    return _real_subprocess_run(cmd, *args, **kwargs)


class TestReadMacosKeychain(unittest.TestCase):
    """The adapter only: unpack `service`, delegate to keychain.py (covered by test_keychain.py)."""

    def test_delegates_service_to_the_shared_reader(self):
        with unittest.mock.patch.object(
            ls, "_read_keychain", return_value="secret-value"
        ) as reader:
            self.assertEqual(
                ls.read_macos_keychain({"service": "MISTRAL_API_KEY"}), "secret-value"
            )
        reader.assert_called_once_with("MISTRAL_API_KEY")

    def test_missing_service_field_returns_none_without_calling_the_reader(self):
        with unittest.mock.patch.object(ls, "_read_keychain") as reader:
            self.assertIsNone(ls.read_macos_keychain({}))
        reader.assert_not_called()


class TestReadAwsSecretsmanager(unittest.TestCase):
    def test_always_returns_none_not_implemented(self):
        self.assertIsNone(
            ls.read_aws_secretsmanager({"secret_id": "prod/x", "env": "X_KEY"})
        )


class TestResolveEnvExports(unittest.TestCase):
    def test_unknown_reader_is_skipped_not_raised(self):
        keys = [{"reader": "vault", "service": "x", "env": "X"}]
        self.assertEqual(ls.resolve_env_exports(keys), [])

    def test_entry_missing_env_is_skipped(self):
        keys = [{"reader": "macos_keychain", "service": "x"}]
        self.assertEqual(ls.resolve_env_exports(keys), [])

    def test_wired_reader_produces_export_line(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="sk-abc123\n", stderr=""
        )
        keys = [
            {
                "reader": "macos_keychain",
                "service": "OPENROUTER_API_KEY",
                "env": "OPENROUTER_API_KEY",
            }
        ]
        with unittest.mock.patch("subprocess.run", return_value=completed):
            self.assertEqual(
                ls.resolve_env_exports(keys), ["export OPENROUTER_API_KEY=sk-abc123"]
            )

    def test_value_with_special_characters_is_shell_quoted(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="va'lue with spaces\n", stderr=""
        )
        keys = [{"reader": "macos_keychain", "service": "X", "env": "X"}]
        with unittest.mock.patch("subprocess.run", return_value=completed):
            lines = ls.resolve_env_exports(keys)
        self.assertEqual(lines, ["export X='va'\"'\"'lue with spaces'"])

    def test_unwired_reader_produces_no_line_but_does_not_raise(self):
        keys = [{"reader": "aws_secretsmanager", "secret_id": "prod/x", "env": "X"}]
        self.assertEqual(ls.resolve_env_exports(keys), [])


class TestLoadKeys(unittest.TestCase):
    def test_returns_empty_list_when_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ls.load_keys(tmp), [])

    def test_returns_keys_array_from_settings_pinky_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude").mkdir()
            keys = [{"reader": "macos_keychain", "service": "X", "env": "X"}]
            (root / ".claude" / "settings.pinky.json").write_text(
                json.dumps({"keys": keys})
            )
            self.assertEqual(ls.load_keys(str(root)), keys)


class TestMainEndToEnd(unittest.TestCase):
    def test_writes_export_lines_to_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude").mkdir()
            keys = [{"reader": "macos_keychain", "service": "X", "env": "X_KEY"}]
            (root / ".claude" / "settings.pinky.json").write_text(
                json.dumps({"keys": keys})
            )
            env_file = root / "env_file.sh"
            env_file.write_text("")  # a sibling hook may already have written here
            with (
                unittest.mock.patch.dict(
                    "os.environ",
                    {"CLAUDE_PROJECT_DIR": str(root), "CLAUDE_ENV_FILE": str(env_file)},
                ),
                unittest.mock.patch("subprocess.run", side_effect=_fake_security_only),
            ):
                self.assertEqual(ls.main(), 0)
            self.assertEqual(env_file.read_text(), "export X_KEY=sk-real\n")

    def test_no_env_file_var_is_a_silent_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.dict(
                "os.environ", {"CLAUDE_PROJECT_DIR": tmp}, clear=False
            ):
                import os

                os.environ.pop("CLAUDE_ENV_FILE", None)
                self.assertEqual(ls.main(), 0)

    def test_appends_rather_than_overwrites_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude").mkdir()
            keys = [{"reader": "macos_keychain", "service": "X", "env": "X_KEY"}]
            (root / ".claude" / "settings.pinky.json").write_text(
                json.dumps({"keys": keys})
            )
            env_file = root / "env_file.sh"
            env_file.write_text("export ALREADY_HERE=1\n")
            with (
                unittest.mock.patch.dict(
                    "os.environ",
                    {"CLAUDE_PROJECT_DIR": str(root), "CLAUDE_ENV_FILE": str(env_file)},
                ),
                unittest.mock.patch("subprocess.run", side_effect=_fake_security_only),
            ):
                ls.main()
            self.assertEqual(
                env_file.read_text(), "export ALREADY_HERE=1\nexport X_KEY=sk-real\n"
            )


if __name__ == "__main__":
    unittest.main()
