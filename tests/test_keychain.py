#!/usr/bin/env python3
"""Tests for plugins/pc-models/hooks/keychain.py — run with:
python3 -m unittest discover tests
"""

import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parent.parent / "plugins" / "pc-models" / "hooks")
)

import keychain as kc


class TestReadMacosKeychain(unittest.TestCase):
    def test_returns_stripped_value_on_success(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="sk-abc123\n", stderr=""
        )
        with unittest.mock.patch("subprocess.run", return_value=completed):
            self.assertEqual(kc.read_macos_keychain("MISTRAL_API_KEY"), "sk-abc123")

    def test_returns_none_when_item_absent_or_locked(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=44, stdout="", stderr="not found"
        )
        with unittest.mock.patch("subprocess.run", return_value=completed):
            self.assertIsNone(kc.read_macos_keychain("MISSING_KEY"))

    def test_returns_none_on_timeout(self):
        with unittest.mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="security", timeout=5),
        ):
            self.assertIsNone(kc.read_macos_keychain("SLOW_KEY"))

    def test_returns_none_when_security_binary_missing(self):
        with unittest.mock.patch(
            "subprocess.run",
            side_effect=OSError("No such file or directory: 'security'"),
        ):
            self.assertIsNone(kc.read_macos_keychain("ANY_KEY"))

    def test_queries_the_given_service_name(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="v\n", stderr=""
        )
        with unittest.mock.patch("subprocess.run", return_value=completed) as run:
            kc.read_macos_keychain("OPENROUTER_API_KEY")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:2], ["security", "find-generic-password"])
        self.assertIn("OPENROUTER_API_KEY", cmd)
        self.assertEqual(cmd[-1], "-w")


if __name__ == "__main__":
    unittest.main()
