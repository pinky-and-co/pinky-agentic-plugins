#!/usr/bin/env python3
"""Tests for plugins/pc-secrets/hooks/secret_patterns.py — run with:
python3 -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parent.parent / "plugins" / "pc-secrets" / "hooks")
)

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
        hardcoded = "password = hunter2-not-an-env-read"  # pragma: allowlist secret
        self.assertIn("secret keyword = value", sp.secret_kinds(hardcoded))


class TestBareTypeAnnotation(unittest.TestCase):
    """A keyword parameter with a bare type annotation is code, not a credential — the shape
    that made a reviewer's Read of typed Python refuse lines like `def _token_path(token: str)`."""

    def test_signature_annotation_is_not_flagged(self):
        for line in (
            "def f(token: str):",
            "def _token_path(token: str, x: int) -> str:",
            "def f(*, access_token: str) -> None:",
            "def g(password: str, secret: str, api_key: str, bearer: str):",
            "def h(aws_secret_access_key: str, refresh_token: str) -> Path:",
            "sorted(items, key=lambda token: token.value)",
        ):
            self.assertEqual(sp.secret_kinds(line), [], line)

    def test_annotation_with_a_real_literal_is_still_flagged(self):
        # Une annotation suivie d'un vrai littéral reste un secret — le littéral compte.
        annotated = 'token: str = "AKIAIOSFODNN7EXAMPLE"'  # pragma: allowlist secret
        arg = 'access_token: str = "ghp_16C7e42F292c6917"'  # pragma: allowlist secret
        self.assertIn("secret keyword = value", sp.secret_kinds(annotated))
        self.assertIn("credential keyword = value", sp.secret_kinds(arg))

    def test_unannotated_forms_keep_their_behaviour(self):
        # Le `=` reste un littéral seul : une chaîne de connexion ou un config `.env` matche
        # toujours, virgule ou parenthèse comprises.
        for line in (
            "password=hunter2,user=bob",  # pragma: allowlist secret
            "api_key=sk-ABCD1234efgh5678ijkl",  # pragma: allowlist secret  gitleaks:allow
            "password: hunter2",  # pragma: allowlist secret
        ):
            self.assertIn("secret keyword = value", sp.secret_kinds(line), line)

    def test_flow_style_colon_value_is_an_accepted_miss(self):
        # `kw: valeur,` est lexiquement identique à une annotation de signature (`token: str,`) :
        # indistinguable au niveau ligne, et la signature gagne — ce miss est le prix assumé du
        # compromis. Les valeurs à préfixe reconnaissable ne dépendent pas de la branche `:`.
        for line in (
            "password: hunter2,",  # pragma: allowlist secret
            "password: hunter2)",  # pragma: allowlist secret
            "password: hunter2 )",  # pragma: allowlist secret
        ):
            self.assertEqual(sp.secret_kinds(line), [], line)
        # Une valeur à tiret (sk-…) n'a pas la forme d'un type nu : la branche `:` la juge
        # toujours, et la nature à préfixe la couvre par ailleurs.
        flagged = "api_key: sk-ABCD1234efgh5678ijkl,"  # pragma: allowlist secret  gitleaks:allow
        self.assertEqual(
            sp.secret_kinds(flagged),
            ["secret keyword = value", "API-key prefix"],
            flagged,
        )


class TestScrubBareTypeAnnotation(unittest.TestCase):
    def test_signature_line_passes_through_untouched(self):
        line = "def _token_path(token: str, x: int) -> str:\n"
        self.assertEqual(sp.scrub(line), line)


class TestScrub(unittest.TestCase):
    def test_redacts_secret_line(self):
        token = "sk-ABCD1234efgh5678ijkl"  # pragma: allowlist secret  gitleaks:allow
        self.assertNotIn(token, sp.scrub(SECRET_LINE))

    def test_leaves_clean_text_untouched(self):
        text = "def add(a, b):\n    return a + b"
        self.assertEqual(sp.scrub(text), text)


if __name__ == "__main__":
    unittest.main()
