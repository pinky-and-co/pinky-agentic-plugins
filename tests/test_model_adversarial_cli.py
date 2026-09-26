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

    def test_out_of_memory_runtime_gpu_form(self):
        # Forme réelle reproduite en direct : 400, pas 503, une fois le modèle déjà chargé —
        # le préflight `out_of_memory` ne couvre pas ce refus mémoire au moment du prompt.
        self.assertTrue(
            ma.is_out_of_memory(
                '400: {"message":"Prompt (3595 tokens) requires ~2775MB GPU memory but '
                'only ~1373MB available. Reduce prompt size or use a smaller model.",'
                '"type":"invalid_request_error","param":null,"code":400}'
            )
        )

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


class TestCliModelIdJoin(unittest.TestCase):
    """`run_pool`'s cli branch must always join `entry["provider"]` onto `entry["model"]` before
    handing it to `pi --model` — the module's own comment documents `'<provider>/<id>'`, but a
    bare `entry["model"]` was passed straight through. Confirmed live: `openai/gpt-oss-120b`
    exists under both `groq` and `openrouter`; with no provider prefix, `pi` silently resolved to
    a third provider, `baseten`, which had no key configured — a routing bug that presented as a
    missing-key error."""

    def _expected_cmd(self, model_arg: str) -> list[str]:
        return [
            "pi",
            "-p",
            "--model",
            model_arg,
            "--no-tools",
            "--no-session",
            "--",
            "p",
        ]

    @unittest.mock.patch("subprocess.run")
    def test_simple_id(self, mock_run):
        entry = {
            "name": "gemini-3.6-flash",
            "kind": "cli",
            "provider": "gemini",
            "model": "gemini-3.6-flash",
            "capacity_tier": "sonnet",
        }
        mock_run.return_value = unittest.mock.MagicMock(
            stdout="ok", stderr="", returncode=0
        )
        ma.run_pool([entry], "p")
        mock_run.assert_called_once_with(
            self._expected_cmd("gemini/gemini-3.6-flash"),
            stdin=subprocess.DEVNULL,
            env=unittest.mock.ANY,
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        )

    @unittest.mock.patch("subprocess.run")
    def test_id_already_containing_a_slash_gets_two_levels_after_the_provider(
        self, mock_run
    ):
        entry = {
            "name": "local-qwen3.5",
            "kind": "cli",
            "provider": "mlx",
            "model": "mlx-community/Qwen3.5-9B-MLX-4bit",
            "capacity_tier": "haiku",
        }
        mock_run.return_value = unittest.mock.MagicMock(
            stdout="ok", stderr="", returncode=0
        )
        ma.run_pool([entry], "p")
        mock_run.assert_called_once_with(
            self._expected_cmd("mlx/mlx-community/Qwen3.5-9B-MLX-4bit"),
            stdin=subprocess.DEVNULL,
            env=unittest.mock.ANY,
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        )

    @unittest.mock.patch("subprocess.run")
    def test_ambiguous_id_is_prefixed_by_its_own_entrys_provider(self, mock_run):
        entry = {
            "name": "groq-gpt-oss-120b",
            "kind": "cli",
            "provider": "groq",
            "model": "openai/gpt-oss-120b",
            "capacity_tier": "sonnet",
        }
        mock_run.return_value = unittest.mock.MagicMock(
            stdout="ok", stderr="", returncode=0
        )
        ma.run_pool([entry], "p")
        mock_run.assert_called_once_with(
            self._expected_cmd("groq/openai/gpt-oss-120b"),
            stdin=subprocess.DEVNULL,
            env=unittest.mock.ANY,
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        )

    @unittest.mock.patch("subprocess.run")
    def test_a_prompt_starting_with_dashes_is_not_mistaken_for_an_option(
        self, mock_run
    ):
        # `build_prompt` always starts with "--- TASK ---" — confirmed live: without a `--`
        # separator, `pi` reads that as an unknown option instead of the positional prompt.
        entry = {
            "name": "mistral-medium-3.5",
            "kind": "cli",
            "provider": "mistral",
            "model": "mistral-medium-3.5",
            "capacity_tier": "sonnet",
        }
        prompt = ma.build_prompt("review this", [])
        mock_run.return_value = unittest.mock.MagicMock(
            stdout="ok", stderr="", returncode=0
        )
        ma.run_pool([entry], prompt)
        called_args = mock_run.call_args[0][0]
        self.assertEqual(called_args[-2:], ["--", prompt])

    @unittest.mock.patch("subprocess.run")
    def test_requested_tools_become_an_allowlist(self, mock_run):
        entry = {
            "name": "groq-gpt-oss-120b",
            "kind": "cli",
            "provider": "groq",
            "model": "openai/gpt-oss-120b",
            "capacity_tier": "sonnet",
        }
        mock_run.return_value = unittest.mock.MagicMock(
            stdout="ok", stderr="", returncode=0
        )
        ma.run_pool([entry], "p", tools=["read", "grep"])
        called_args = mock_run.call_args[0][0]
        self.assertEqual(called_args[4:6], ["-t", "read,grep"])
        self.assertNotIn("--no-tools", called_args)


class TestModelAdversarialCLI(unittest.TestCase):
    def setUp(self):
        self.cli_entry = {
            "name": "pi-model",
            "kind": "cli",
            "provider": "anthropic",
            "model": "claude-3-5-sonnet",
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

        # Le champ `tools` de l'entrée n'est plus lu : les outils se demandent à l'appel.
        expected_cmd = [
            "pi",
            "-p",
            "--model",
            "anthropic/claude-3-5-sonnet",
            "--no-tools",
            "--no-session",
            "--",
            self.prompt,
        ]
        mock_run.assert_called_once_with(
            expected_cmd,
            stdin=subprocess.DEVNULL,
            env=unittest.mock.ANY,
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        )

        self.assertEqual(result.entry, self.cli_entry)
        self.assertEqual(result.result, "The code is correct.")

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
        self.assertEqual(result.result, "Success on second try")

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
