#!/usr/bin/env python3
"""Tests for plugins/pc-core/hooks/secret_patterns.py — run with:
python3 -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "hooks"))

import secret_patterns as sp

SECRET_LINE = (
    "api_key = sk-ABCD1234efgh5678ijkl"  # pragma: allowlist secret  gitleaks:allow
)


class TestSecretKinds(unittest.TestCase):
    def test_finds_api_key_prefix(self):
        self.assertIn("API-key prefix", sp.secret_kinds(SECRET_LINE))

    def test_clean_text_returns_empty(self):
        self.assertEqual(sp.secret_kinds("def add(a, b):\n    return a + b"), [])

    def test_finds_credentials_in_url(self):
        url = "https://user:pass@example.com/x"  # pragma: allowlist secret
        self.assertIn("credentials in URL", sp.secret_kinds(url))

    def test_finds_pem_header(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----"  # pragma: allowlist secret
        self.assertIn("PEM private-key header", sp.secret_kinds(pem))

    def test_env_var_read_not_flagged(self):
        # Lire un secret depuis l'environnement n'est pas une fuite — aucune valeur littérale.
        for line in (
            'api_key=os.environ["ANTHROPIC_API_KEY"]',
            'token = os.environ.get("GITHUB_TOKEN")',
            'secret = os.getenv("CLIENT_SECRET")',
            "api_key = process.env.OPENAI_API_KEY",
        ):
            self.assertEqual(sp.secret_kinds(line), [], line)

    def test_hardcoded_secret_still_flagged(self):
        # L'exception env-var ne doit pas avaler une vraie valeur codée en dur.
        self.assertIn(
            "secret keyword = value",
            sp.secret_kinds("password = hunter2-not-an-env-read"),
        )


class TestScrub(unittest.TestCase):
    def test_redacts_secret_line(self):
        token = "sk-ABCD1234efgh5678ijkl"  # pragma: allowlist secret  gitleaks:allow
        self.assertNotIn(token, sp.scrub(SECRET_LINE))

    def test_leaves_clean_text_untouched(self):
        text = "def add(a, b):\n    return a + b"
        self.assertEqual(sp.scrub(text), text)


if __name__ == "__main__":
    unittest.main()
