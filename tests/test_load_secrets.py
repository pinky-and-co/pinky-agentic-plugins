#!/usr/bin/env python3
"""Tests for plugins/pc-core/hooks/load_secrets.py — run with:
python3 -m unittest discover tests
"""

import io
import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "hooks"))

import load_secrets as ls


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


class TestResolveEnv(unittest.TestCase):
    def test_resolved_entries_become_a_name_to_value_mapping(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="sk-abc123\n", stderr=""
        )
        keys = [
            {
                "reader": "macos_keychain",
                "service": "GROQ_API_KEY",
                "env": "GROQ_API_KEY",
            },
            {"reader": "vault", "service": "x", "env": "SKIPPED"},
        ]
        with unittest.mock.patch("subprocess.run", return_value=completed):
            self.assertEqual(
                ls.resolve_env(keys),
                {"GROQ_API_KEY": "sk-abc123"},  # pragma: allowlist secret
            )

    def test_entry_missing_env_is_skipped(self):
        keys = [{"reader": "macos_keychain", "service": "X"}]
        with unittest.mock.patch.object(ls, "_read_keychain") as reader:
            self.assertEqual(ls.resolve_env(keys), {})
        reader.assert_not_called()

    def test_leftover_literal_entry_resolves_to_nothing_and_fails_loud_on_stderr(self):
        keys = [{"reader": "literal", "value": "41c2b051", "env": "ACCOUNT_ID"}]
        stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            env = ls.resolve_env(keys)
        self.assertEqual(env, {})
        self.assertEqual(stderr.getvalue(), "[load_secrets] unknown reader: literal\n")

    def test_unwired_reader_resolves_to_nothing_and_fails_loud_on_stderr(self):
        keys = [{"reader": "aws_secretsmanager", "secret_id": "prod/x", "env": "X"}]
        stderr = io.StringIO()
        with unittest.mock.patch("sys.stderr", stderr):
            env = ls.resolve_env(keys)
        self.assertEqual(env, {})
        self.assertEqual(
            stderr.getvalue(),
            "[load_secrets] reader aws_secretsmanager: not implemented yet "
            "(entry for env=X)\n",
        )


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


if __name__ == "__main__":
    unittest.main()
