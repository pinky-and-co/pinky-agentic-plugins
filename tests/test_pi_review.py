#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/skills/kanban/scripts/pi_review.py."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-kanban"
    / "skills"
    / "kanban"
    / "scripts"
    / "pi_review.py"
)

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp"))
sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-kanban"
        / "skills"
        / "kanban"
        / "scripts"
    ),
)
import pi_review  # noqa: E402
from pi_fakes import PiPopen  # noqa: E402

CARD_TWO_POINTS = """---
date: 2026-09-22
---
# Some card

## Ce qu'on corrige

Context here.

## Done when =

### 1. First thing

Detail of the first thing.

### 2. Second thing

Detail of the second thing.

## Ce qu'on ne fait pas

Not this.
"""

CARD_NO_SUBHEADINGS = """---
date: 2026-09-22
---
# Some card

## Done when

Just a flat paragraph, no numbered subheadings.

## Gates

irrelevant.
"""

CARD_NO_DONE_WHEN = """---
date: 2026-09-22
---
# Some card

## Ce qu'on corrige

No Done when section at all.
"""

CLI_ENTRY = {
    "name": "local-qwen3.5",
    "kind": "cli",
    "provider": "mlx",
    "model": "mlx-community/Qwen3.5-9B-MLX-4bit",
    "capacity_tier": "haiku",
    "free_tier": True,
    "concurrency": 1,
    "local": True,
    "tools": "bash",  # ignoré : les outils se passent à l'appel, jamais lus dans le pool
}
CLI_ENTRY_2 = {
    "name": "openrouter-free",
    "kind": "cli",
    "provider": "openrouter",
    "model": "free",
    "capacity_tier": "haiku",
    "free_tier": True,
    "concurrency": None,
}
HTTP_ENTRY = {
    "name": "cohere-command-r7b",
    "kind": "http",
    "provider": "cohere",
    "model": "command-r7b-12-2024",
    "base_url": "https://api.cohere.com/compatibility/v1/chat/completions",
    "api_key_env": "COHERE_API_KEY",  # pragma: allowlist secret
    "capacity_tier": "haiku",
    "free_tier": True,
    "concurrency": None,
}
OFFLINE_ENTRY = {
    "name": "local-ollama",
    "kind": "offline",
    "provider": "ollama",
    "model": None,
    "capacity_tier": "haiku",
    "free_tier": True,
    "concurrency": 1,
}


class TestExtractDoneWhen(unittest.TestCase):
    def test_finds_section(self):
        self.assertIn("First thing", pi_review.extract_done_when(CARD_TWO_POINTS))
        self.assertNotIn("ne fait pas", pi_review.extract_done_when(CARD_TWO_POINTS))

    def test_absent_returns_empty(self):
        self.assertEqual(pi_review.extract_done_when(CARD_NO_DONE_WHEN), "")


class TestSplitPoints(unittest.TestCase):
    def test_numbered_subheadings(self):
        done_when = pi_review.extract_done_when(CARD_TWO_POINTS)
        points = pi_review.split_points(done_when)
        self.assertEqual([t for t, _ in points], ["First thing", "Second thing"])
        self.assertIn("Detail of the first", points[0][1])
        self.assertIn("Detail of the second", points[1][1])

    def test_no_subheadings_returns_whole_section_as_one_point(self):
        done_when = pi_review.extract_done_when(CARD_NO_SUBHEADINGS)
        points = pi_review.split_points(done_when)
        self.assertEqual(len(points), 1)
        self.assertIn("flat paragraph", points[0][1])

    def test_empty_section_returns_no_points(self):
        self.assertEqual(pi_review.split_points(""), [])


class TestReviewCandidates(unittest.TestCase):
    def test_excludes_http_and_offline(self):
        pool = [HTTP_ENTRY, OFFLINE_ENTRY, CLI_ENTRY]
        entries = pi_review.review_candidates(pool)
        self.assertEqual([e["name"] for e in entries], ["local-qwen3.5"])


class TestReviewPoint(unittest.TestCase):
    @unittest.mock.patch("model_adversarial.Popen", new_callable=PiPopen)
    def test_ok_status_parsed(self, mock_run):
        mock_run.return_value = unittest.mock.MagicMock(
            stdout="STATUS: OK\nJUSTIFICATION: file.py line 3 does it.",
            stderr="",
            returncode=0,
        )
        entries = pi_review.review_candidates([CLI_ENTRY])
        constat = pi_review.review_point(entries, "/repo", "First thing", "detail", 60)
        self.assertEqual(constat["status"], "OK")
        self.assertEqual(constat["model"], "local-qwen3.5")
        self.assertIn("file.py line 3", constat["detail"])

    @unittest.mock.patch("model_adversarial.Popen", new_callable=PiPopen)
    def test_gap_status_parsed(self, mock_run):
        mock_run.return_value = unittest.mock.MagicMock(
            stdout="STATUS: GAP\nJUSTIFICATION: missing in file.py.",
            stderr="",
            returncode=0,
        )
        entries = pi_review.review_candidates([CLI_ENTRY])
        constat = pi_review.review_point(entries, "/repo", "First thing", "detail", 60)
        self.assertEqual(constat["status"], "GAP")

    @unittest.mock.patch("model_adversarial.Popen", new_callable=PiPopen)
    def test_unparseable_response_is_error_not_fabricated(self, mock_run):
        mock_run.return_value = unittest.mock.MagicMock(
            stdout="I refuse to answer in that format.", stderr="", returncode=0
        )
        entries = pi_review.review_candidates([CLI_ENTRY])
        constat = pi_review.review_point(entries, "/repo", "First thing", "detail", 60)
        self.assertEqual(constat["status"], "ERROR")
        self.assertIn("unparseable", constat["detail"])

    @unittest.mock.patch("model_adversarial.Popen", new_callable=PiPopen)
    def test_429_on_first_model_falls_over_to_second(self, mock_run):
        mock_run.side_effect = [
            subprocess.CalledProcessError(
                1, "pi", output="", stderr="429: rate limited"
            ),
            unittest.mock.MagicMock(
                stdout="STATUS: OK\nJUSTIFICATION: second model served.",
                stderr="",
                returncode=0,
            ),
        ]
        entries = pi_review.review_candidates([CLI_ENTRY, CLI_ENTRY_2])
        constat = pi_review.review_point(entries, "/repo", "First thing", "detail", 60)
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(constat["status"], "OK")
        self.assertEqual(constat["model"], "openrouter-free")

    @unittest.mock.patch("model_adversarial.Popen", new_callable=PiPopen)
    def test_503_out_of_memory_falls_over_to_second(self, mock_run):
        mock_run.side_effect = [
            subprocess.CalledProcessError(
                1, "pi", output="", stderr='503: {"error":{"type":"out_of_memory"}}'
            ),
            unittest.mock.MagicMock(
                stdout="STATUS: OK\nJUSTIFICATION: second model served.",
                stderr="",
                returncode=0,
            ),
        ]
        entries = pi_review.review_candidates([CLI_ENTRY, CLI_ENTRY_2])
        constat = pi_review.review_point(entries, "/repo", "First thing", "detail", 60)
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(constat["status"], "OK")
        self.assertEqual(constat["model"], "openrouter-free")

    @unittest.mock.patch("model_adversarial.Popen", new_callable=PiPopen)
    def test_other_error_is_reported_without_fallover(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "pi", output="", stderr="401: invalid api key"
        )
        entries = pi_review.review_candidates([CLI_ENTRY, CLI_ENTRY_2])
        constat = pi_review.review_point(entries, "/repo", "First thing", "detail", 60)
        # un seul appel : la deuxième entrée du pool n'est jamais tentée sur une erreur
        # qui n'est ni un 429 ni un 503 out_of_memory.
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(constat["status"], "ERROR")
        self.assertIsNone(constat["model"])

    @unittest.mock.patch("model_adversarial.Popen", new_callable=PiPopen)
    def test_pool_exhausted_is_error_not_fabricated(self, mock_run):
        mock_run.side_effect = [
            subprocess.CalledProcessError(
                1, "pi", output="", stderr="429: rate limited"
            ),
            subprocess.CalledProcessError(
                1, "pi", output="", stderr='503: {"error":{"type":"out_of_memory"}}'
            ),
        ]
        entries = pi_review.review_candidates([CLI_ENTRY, CLI_ENTRY_2])
        constat = pi_review.review_point(entries, "/repo", "First thing", "detail", 60)
        # les deux entrées sont tentées, aucun verdict n'est fabriqué une fois épuisées.
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(constat["status"], "ERROR")
        self.assertIsNone(constat["model"])

    @unittest.mock.patch("model_adversarial.Popen", new_callable=PiPopen)
    def test_full_command_is_constructed_through_review_point(self, mock_run):
        # Oracle sur la ligne de commande réelle construite par review_point → run_pool,
        # pas seulement sur le verdict — la card exige explicitement --mode json, -t read et --model <provider>/<id>.
        mock_run.return_value = unittest.mock.MagicMock(
            stdout="STATUS: OK\nJUSTIFICATION: fine.", stderr="", returncode=0
        )
        entries = pi_review.review_candidates([CLI_ENTRY])
        pi_review.review_point(entries, "/repo", "First thing", "detail", 60)
        mock_run.assert_called_once_with(
            [
                "pi",
                "--mode",
                "json",
                "--model",
                "mlx/mlx-community/Qwen3.5-9B-MLX-4bit",
                "-t",
                "read",
                "--",
                pi_review.build_prompt("/repo", "First thing", "detail"),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=unittest.mock.ANY,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )


class TestScriptImportsStandalone(unittest.TestCase):
    """Runs the script as a real subprocess, with none of this test file's own `sys.path`
    seeding — the only thing that would have caught the `model_adversarial` import path being
    wrong (`parent.parent.parent.parent` landed one level short, inside `pc-kanban/` instead of
    `plugins/`). No pool call needed: a card with no `## Done when` fails before touching the
    pool, but only after the module-level `import model_adversarial` has already run."""

    def test_no_done_when_card_fails_cleanly_not_with_a_traceback(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            card = Path(tmp) / "card.md"
            card.write_text(CARD_NO_DONE_WHEN, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), str(card), "/repo"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertNotIn("ModuleNotFoundError", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)


class TestMain(unittest.TestCase):
    """Exercises the real `policy_lib` ascending walk for `load_pool` — same convention as
    `test_model_adversarial.py::TestLoadPool` — no mock of `load_pool` itself, only of the `pi`
    subprocess boundary."""

    def setUp(self):
        import os
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.project_dir = Path(self._tmp.name)
        self._original_project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.project_dir)

    def tearDown(self):
        import os

        if self._original_project_dir is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = self._original_project_dir
        self._tmp.cleanup()

    def _write_card(self, text: str) -> str:
        card = self.project_dir / "card.md"
        card.write_text(text, encoding="utf-8")
        return str(card)

    def _write_pool(self, pool: list[dict]) -> None:
        (self.project_dir / ".claude").mkdir(exist_ok=True)
        (self.project_dir / ".claude" / "settings.pinky.json").write_text(
            json.dumps({"model_pool": pool})
        )

    @unittest.mock.patch("model_adversarial.Popen", new_callable=PiPopen)
    def test_end_to_end_one_constat_per_point(self, mock_run):
        card_path = self._write_card(CARD_TWO_POINTS)
        self._write_pool([CLI_ENTRY])
        mock_run.return_value = unittest.mock.MagicMock(
            stdout="STATUS: OK\nJUSTIFICATION: fine.", stderr="", returncode=0
        )
        buf = []
        with unittest.mock.patch("builtins.print", lambda s: buf.append(s)):
            rc = pi_review.main([card_path, "/repo"])
        self.assertEqual(rc, 0)
        constats = json.loads(buf[0])
        self.assertEqual(len(constats), 2)
        self.assertEqual(constats[0]["point"], "First thing")
        self.assertEqual(constats[1]["point"], "Second thing")

    def test_no_done_when_section_returns_1(self):
        card_path = self._write_card(CARD_NO_DONE_WHEN)
        self.assertEqual(pi_review.main([card_path, "/repo"]), 1)

    def test_no_cli_candidate_returns_1(self):
        card_path = self._write_card(CARD_TWO_POINTS)
        self._write_pool([HTTP_ENTRY, OFFLINE_ENTRY])
        self.assertEqual(pi_review.main([card_path, "/repo"]), 1)

    def test_missing_card_returns_1_not_a_traceback(self):
        self._write_pool([CLI_ENTRY])
        buf = []
        with unittest.mock.patch("builtins.print", lambda s: buf.append(s)):
            rc = pi_review.main([str(self.project_dir / "absent.md"), "/repo"])
        self.assertEqual(rc, 1)
        self.assertIn("cannot read card", buf[0])


class TestDefaultTimeout(unittest.TestCase):
    def test_the_tool_loop_gets_the_pools_wall_clock_backstop(self):
        # `review_point` passe des outils : une boucle d'exploration dure des minutes.
        self.assertEqual(pi_review.DEFAULT_TIMEOUT, pi_review.ma.RUN_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
