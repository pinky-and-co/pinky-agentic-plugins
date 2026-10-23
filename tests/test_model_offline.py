#!/usr/bin/env python3
"""Tests for `model_offline.py` — MLX-SERVE via `pi`, zero cloud egress.

Mocks only `subprocess.Popen` (the outside boundary — the `pi` binary); never a function internal to
the module under test.
"""

import errno
import io
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from pi_fakes import MlxPopen

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-models" / "mcp"))
import model_offline as mo  # noqa: E402


class TestResolveModel(unittest.TestCase):
    def test_default_is_mlx_id(self):
        self.assertEqual(mo.DEFAULT_MODEL, "ornith-ai/Ornith-1.5-9B-MLX-6bit")

    def test_cli_overrides_env_and_default(self):
        with unittest.mock.patch.dict(
            "os.environ", {"PINKY_LOCAL_MODEL": "mlx-community/other"}
        ):
            self.assertEqual(
                mo.resolve_model("mlx-community/cli-wins"), "mlx-community/cli-wins"
            )

    def test_env_overrides_default(self):
        with unittest.mock.patch.dict(
            "os.environ", {"PINKY_LOCAL_MODEL": "mlx-community/env-wins"}
        ):
            self.assertEqual(mo.resolve_model(None), "mlx-community/env-wins")

    def test_falls_back_to_default(self):
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(mo.resolve_model(None), mo.DEFAULT_MODEL)


class TestBuildPrompt(unittest.TestCase):
    def test_oracle_task_only(self):
        self.assertEqual(
            mo.build_prompt("summarize this", []),
            "You are a local assistant. Perform the TASK on the material below; answer fully and "
            "directly.\n\n--- TASK ---\nsummarize this",
        )

    def test_oracle_with_file(self):
        file_path = Path(tempfile.mkdtemp()) / "x.txt"
        file_path.write_text("file body", encoding="utf-8")
        result = mo.build_prompt("task text", [str(file_path)])
        self.assertEqual(
            result,
            "You are a local assistant. Perform the TASK on the material below; answer fully and "
            f"directly.\n\n--- TASK ---\ntask text\n\n--- FILE: {file_path} ---\nfile body",
        )


class TestIsOutOfMemory(unittest.TestCase):
    def test_detects_marker(self):
        self.assertTrue(mo.is_out_of_memory('{"error":{"type":"out_of_memory"}}'))

    def test_absent(self):
        self.assertFalse(mo.is_out_of_memory('{"error":{"type":"invalid_model"}}'))

    def test_detects_runtime_gpu_memory_form(self):
        # Forme réelle reproduite en direct : 400, pas 503, une fois le modèle déjà chargé —
        # le préflight `out_of_memory` ne couvre pas ce refus mémoire au moment du prompt.
        self.assertTrue(
            mo.is_out_of_memory(
                '400: {"message":"Prompt (3595 tokens) requires ~2775MB GPU memory but '
                'only ~1373MB available. Reduce prompt size or use a smaller model.",'
                '"type":"invalid_request_error","param":null,"code":400}'
            )
        )


class TestCallMlx(unittest.TestCase):
    """`call_mlx` must build the exact `pi` command `run_pool`'s cli branch already uses."""

    @unittest.mock.patch("subprocess.Popen", new_callable=MlxPopen)
    def test_command_shape_and_stdout(self, mock_run):
        mock_run.return_value = unittest.mock.MagicMock(
            stdout="the answer", stderr="", returncode=0
        )
        result = mo.call_mlx(
            "mlx-community/gemma-4-12b-it-4bit", "do the task", timeout=120
        )
        mock_run.assert_called_once_with(
            [
                "pi",
                "-p",
                "--model",
                "mlx/mlx-community/gemma-4-12b-it-4bit",
                "-t",
                "read",
                "--thinking",
                "off",
                "do the task",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.assertEqual(result, "the answer")


class TestFailureHint(unittest.TestCase):
    def test_out_of_memory_is_blocked(self):
        exc = subprocess.CalledProcessError(
            1, "pi", output="", stderr='{"error":{"type":"out_of_memory"}}'
        )
        code, hint = mo.failure_hint(exc, "m", 900)
        self.assertEqual(code, 2)
        self.assertEqual(
            hint,
            "local MLX server out of memory (m); "
            "shrink the --file inputs or free RAM on the server.",
        )  # oracle complet : la chaîne fixe EST l'invariant du module (zéro égress)
        self.assertNotIn(
            "out_of_memory", hint
        )  # jamais le corps brut de pi dans le hint

    def test_generic_called_process_error_is_blocked(self):
        exc = subprocess.CalledProcessError(3, "pi", output="", stderr="boom")
        code, hint = mo.failure_hint(exc, "m", 900)
        self.assertEqual(code, 2)
        self.assertNotIn("boom", hint)  # jamais le stderr brut de pi dans le hint

    def test_timeout_is_error(self):
        exc = subprocess.TimeoutExpired(cmd="pi", timeout=900)
        code, hint = mo.failure_hint(exc, "m", 900)
        self.assertEqual(code, 1)
        self.assertIn("900", hint)

    def test_file_not_found_is_blocked(self):
        code, hint = mo.failure_hint(FileNotFoundError(), "m", 900)
        self.assertEqual(code, 2)
        self.assertIn("pi", hint)

    def test_other_os_error_is_blocked_never_raises(self):
        code, hint = mo.failure_hint(OSError("weird"), "m", 900)
        self.assertEqual(code, 2)
        self.assertNotIn(
            "weird", hint
        )  # jamais le message brut d'exception, potentiellement sensible

    def test_e2big_blames_argv_limit_not_mlx_serve(self):
        exc = OSError("Argument list too long")
        exc.errno = errno.E2BIG
        code, hint = mo.failure_hint(exc, "m", 900)
        self.assertEqual(code, 2)
        self.assertNotIn("MLX-SERVE", hint)
        self.assertIn("argv", hint.lower())


class TestMainZeroEgress(unittest.TestCase):
    """`main()` is the real code path — never a hand-rolled duplicate of its logic."""

    @unittest.mock.patch("subprocess.Popen", new_callable=MlxPopen)
    def test_success_writes_body_to_file_prints_only_status(self, mock_run):
        mock_run.return_value = unittest.mock.MagicMock(
            stdout="SENSITIVE ANSWER BODY", stderr="", returncode=0
        )
        out_path = Path(tempfile.mkdtemp()) / "answer.md"
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = mo.main(["--task", "do it", "--out", str(out_path)])
        self.assertEqual(code, 0)
        printed = out.getvalue()
        self.assertNotIn("SENSITIVE ANSWER BODY", printed)
        self.assertIn(str(out_path), printed)
        self.assertEqual(out_path.read_text(encoding="utf-8"), "SENSITIVE ANSWER BODY")

    @unittest.mock.patch("subprocess.Popen", new_callable=MlxPopen)
    def test_pi_failure_never_leaks_stdout_stderr_and_exits_blocked(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "pi", output="ECHOED SENSITIVE PROMPT", stderr="ALSO SENSITIVE"
        )
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = mo.main(["--task", "do it"])
        self.assertEqual(code, 2)
        printed = err.getvalue()
        self.assertNotIn("ECHOED SENSITIVE PROMPT", printed)
        self.assertNotIn("ALSO SENSITIVE", printed)
        self.assertIn("BLOCKED", printed)

    @unittest.mock.patch("subprocess.Popen", new_callable=MlxPopen)
    def test_out_of_memory_failure_exits_blocked_with_hint(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "pi", output="", stderr='{"error":{"type":"out_of_memory"}}'
        )
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = mo.main(["--task", "do it"])
        self.assertEqual(code, 2)
        self.assertIn("out of memory", err.getvalue())

    @unittest.mock.patch("subprocess.Popen", new_callable=MlxPopen)
    def test_timeout_exits_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="pi", timeout=900)
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = mo.main(["--task", "do it"])
        self.assertEqual(code, 1)
        self.assertIn("ERROR", err.getvalue())

    @unittest.mock.patch("subprocess.Popen", new_callable=MlxPopen)
    def test_model_override_flows_into_pi_command(self, mock_run):
        mock_run.return_value = unittest.mock.MagicMock(
            stdout="ok", stderr="", returncode=0
        )
        out_path = Path(tempfile.mkdtemp()) / "answer.md"
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            mo.main(
                [
                    "--task",
                    "do it",
                    "--model",
                    "mlx-community/other-model",
                    "--out",
                    str(out_path),
                ]
            )
        called_cmd = mock_run.call_args[0][0]
        self.assertIn("mlx/mlx-community/other-model", called_cmd)


if __name__ == "__main__":
    unittest.main()
