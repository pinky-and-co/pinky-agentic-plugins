#!/usr/bin/env python3
import unittest
import unittest.mock
import subprocess
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp"))
import model_adversarial as ma


class TestFailoverPredicates(unittest.TestCase):
    """One criterion: bascule when the provider refuses to serve, never when it served but
    answered badly."""

    def test_rate_limited(self):
        self.assertTrue(
            ma.is_rate_limited("Mistral API error (429): too many requests")
        )
        self.assertFalse(ma.is_rate_limited("Mistral API error (400): invalid_model"))

    def test_out_of_memory(self):
        self.assertTrue(ma.is_out_of_memory('{"error":{"type":"out_of_memory"}}'))
        self.assertFalse(ma.is_out_of_memory('{"error":{"type":"invalid_model"}}'))

    def test_insufficient_credit(self):
        self.assertTrue(
            ma.is_insufficient_credit(
                '402: {"message":"This request requires more credits, or fewer '
                "max_tokens. You requested up to 65536 tokens, but can only afford "
                '10666"}'
            )
        )
        self.assertFalse(
            ma.is_insufficient_credit("Mistral API error (400): invalid_model")
        )

    def test_timeout(self):
        self.assertTrue(ma.is_timeout("Command 'pi' timed out after 180 seconds"))
        self.assertFalse(ma.is_timeout("Mistral API error (400): invalid_model"))


class TestModelAdversarialCLI(unittest.TestCase):
    def setUp(self):
        self.cli_entry = {
            "name": "pi-model",
            "kind": "cli",
            "model": "anthropic/claude-3-5-sonnet",
            "tools": "read",
            "capacity_tier": "opus",
        }
        self.prompt = "review this code"

    @unittest.mock.patch("subprocess.run")
    def test_cli_success(self, mock_run):
        # Mock successful pi call
        mock_run.return_value = unittest.mock.MagicMock(
            stdout="The code is correct.", stderr="", returncode=0
        )

        result = ma.run_pool([self.cli_entry], self.prompt)

        # Verify the command structure
        expected_cmd = [
            "pi",
            "-p",
            "--model",
            "anthropic/claude-3-5-sonnet",
            "-t",
            "read",
            "--no-session",
            "--thinking",
            "off",
            self.prompt,
        ]
        mock_run.assert_called_once_with(
            expected_cmd, capture_output=True, text=True, timeout=180, check=True
        )

        self.assertEqual(result.entry, self.cli_entry)
        self.assertEqual(
            result.result["choices"][0]["message"]["content"], "The code is correct."
        )

    @unittest.mock.patch("subprocess.run")
    def test_cli_failover_on_rate_limit(self, mock_run):
        # Pool de deux modèles cli pour tester la bascule. Le premier appel échoue avec un 429
        # au format Mistral réel, le second réussit.
        pool = [self.cli_entry, {**self.cli_entry, "name": "pi-model-2"}]
        mock_run.side_effect = [
            subprocess.CalledProcessError(
                1, "pi", stderr="Mistral API error (429): too many requests"
            ),
            unittest.mock.MagicMock(
                stdout="Success on second try", stderr="", returncode=0
            ),
        ]

        result = ma.run_pool(pool, self.prompt)

        self.assertEqual(result.entry["name"], "pi-model-2")
        self.assertEqual(
            result.result["choices"][0]["message"]["content"], "Success on second try"
        )

    @unittest.mock.patch("subprocess.run")
    def test_cli_failover_on_oom(self, mock_run):
        # Forme réelle du refus préflight de MLX-SERVE : corps 503 portant out_of_memory.
        pool = [self.cli_entry, {**self.cli_entry, "name": "pi-model-2"}]
        mock_run.side_effect = [
            subprocess.CalledProcessError(
                1, "pi", stderr='{"error":{"type":"out_of_memory"}}'
            ),
            unittest.mock.MagicMock(stdout="Success", stderr="", returncode=0),
        ]

        result = ma.run_pool(pool, self.prompt)
        self.assertEqual(result.entry["name"], "pi-model-2")

    @unittest.mock.patch("subprocess.run")
    def test_cli_failover_on_insufficient_credit(self, mock_run):
        pool = [self.cli_entry, {**self.cli_entry, "name": "pi-model-2"}]
        mock_run.side_effect = [
            subprocess.CalledProcessError(
                1, "pi", stderr='402: {"message":"insufficient credit"}'
            ),
            unittest.mock.MagicMock(stdout="Success", stderr="", returncode=0),
        ]

        result = ma.run_pool(pool, self.prompt)
        self.assertEqual(result.entry["name"], "pi-model-2")

    @unittest.mock.patch("subprocess.run")
    def test_cli_failover_matches_stdout_too_not_only_stderr(self, mock_run):
        # Un fournisseur qui écrit son erreur sur stdout, pas stderr, doit quand même être
        # détecté — les deux flux combinés sont vérifiés, jamais stderr seul.
        pool = [self.cli_entry, {**self.cli_entry, "name": "pi-model-2"}]
        mock_run.side_effect = [
            subprocess.CalledProcessError(
                1, "pi", output="Mistral API error (429): too many requests", stderr=""
            ),
            unittest.mock.MagicMock(stdout="Success", stderr="", returncode=0),
        ]

        result = ma.run_pool(pool, self.prompt)
        self.assertEqual(result.entry["name"], "pi-model-2")

    @unittest.mock.patch("subprocess.run")
    def test_cli_critical_failure(self, mock_run):
        # Error that is NOT in the fail-over list
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd="pi", stderr="Unexpected internal crash"
        )

        result = ma.run_pool([self.cli_entry], self.prompt)

        self.assertIsNone(result.entry)
        self.assertIn("critical failure", result.result)
        self.assertIn("Unexpected internal crash", result.result)

    @unittest.mock.patch("subprocess.run")
    def test_cli_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="pi", timeout=180)

        pool = [self.cli_entry, {**self.cli_entry, "name": "pi-model-2"}]
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="pi", timeout=180),
            unittest.mock.MagicMock(stdout="Success", stderr="", returncode=0),
        ]

        result = ma.run_pool(pool, self.prompt)
        self.assertEqual(result.entry["name"], "pi-model-2")


if __name__ == "__main__":
    unittest.main()
