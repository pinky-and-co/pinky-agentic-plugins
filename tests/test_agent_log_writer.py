#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/hooks/agent_log_writer.py — run with:
python3 -m unittest discover tests
"""

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parent.parent / "plugins" / "pc-kanban" / "hooks")
)

import agent_log_writer as alw


def _tool(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


def _write_transcript(path: Path, entries: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _user(content):
    return {"type": "user", "message": {"role": "user", "content": content}}


def _assistant(*blocks):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": list(blocks)},
    }


class ScanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transcript = Path(self.tmp.name) / "t.jsonl"

    def scan(self, *entries):
        return alw.scan_transcript(_write_transcript(self.transcript, list(entries)))


class TestDidWork(ScanCase):
    def test_bash_git_commit_is_work(self):
        s = self.scan(_assistant(_tool("Bash", command='git commit -m "✨ feat: x"')))
        self.assertTrue(s.did_work)

    def test_git_status_is_not_work(self):
        s = self.scan(_assistant(_tool("Bash", command="git status --short")))
        self.assertFalse(s.did_work)

    def test_commit_skill_is_work(self):
        s = self.scan(_assistant(_tool("Skill", skill="pc-git:commit-and-pr")))
        self.assertTrue(s.did_work)

    def test_card_edit_is_work(self):
        s = self.scan(
            _assistant(
                _tool("Write", file_path="/w/_KANBAN/CHANTIER0001-x/1-todo/c.md")
            )
        )
        self.assertTrue(s.did_work)

    def test_card_mv_is_work(self):
        s = self.scan(
            _assistant(
                _tool("Bash", command="mv _KANBAN/C/1-todo/c.md _KANBAN/C/4-done/")
            )
        )
        self.assertTrue(s.did_work)

    def test_code_edit_alone_is_not_work(self):
        s = self.scan(_assistant(_tool("Edit", file_path="/repo/setup/cli.py")))
        self.assertFalse(s.did_work)


class TestWroteLog(ScanCase):
    def test_write_under_agent_log(self):
        s = self.scan(
            _assistant(_tool("Write", file_path="/w/_AGENT_LOG/2026-09-23_x.md"))
        )
        self.assertTrue(s.wrote_log)

    def test_shell_redirect_into_agent_log(self):
        s = self.scan(
            _assistant(_tool("Bash", command="cat >> _AGENT_LOG/2026-09-23_x.md <<EOF"))
        )
        self.assertTrue(s.wrote_log)

    def test_reading_agent_log_is_not_writing(self):
        s = self.scan(_assistant(_tool("Bash", command="ls _AGENT_LOG/")))
        self.assertFalse(s.wrote_log)

    def test_nested_agent_log_write_also_matches_kanban_but_wrote_log_wins(self):
        """`_AGENT_LOG/` now nests under `_KANBAN/`, so a log-only write also matches the
        `_KANBAN` substring — `did_work` turns True alongside `wrote_log`. `main()`'s own gate
        (`not did_work or wrote_log`) short-circuits on `wrote_log` regardless, so this stays a
        no-op for the writer."""
        s = self.scan(
            _assistant(
                _tool("Write", file_path="/w/_KANBAN/_AGENT_LOG/2026-09-23_x.md")
            )
        )
        self.assertTrue(s.did_work)
        self.assertTrue(s.wrote_log)


class TestExcerpt(ScanCase):
    def test_keeps_text_and_paths_drops_tool_results(self):
        s = self.scan(
            _user("fais le fix"),
            _assistant(
                {"type": "text", "text": "Je corrige."},
                _tool("Edit", file_path="/r/a.py"),
            ),
            _user([{"type": "tool_result", "content": "SECRET OUTPUT"}]),
        )
        joined = "\n".join(s.lines)
        self.assertIn("USER: fais le fix", joined)
        self.assertIn("ASSISTANT: Je corrige.", joined)
        self.assertIn("EDITED: /r/a.py", joined)
        self.assertNotIn("SECRET OUTPUT", joined)

    def test_strips_system_reminders_and_meta(self):
        s = self.scan(
            _user("vrai message<system-reminder>bruit</system-reminder>"),
            {**_user("caveat"), "isMeta": True},
        )
        self.assertEqual(s.lines, ["USER: vrai message"])

    def test_excerpt_is_scrubbed_and_capped_from_the_end(self):
        with unittest.mock.patch.object(
            alw, "scrub", side_effect=lambda t: t.replace("tok", "[redacted]")
        ):
            text = alw.build_excerpt(["tok"] + ["x" * 100] * 1000)
        self.assertTrue(text.startswith("[…]\n"))
        self.assertLessEqual(len(text), alw.MAX_EXCERPT_CHARS + 4)
        with unittest.mock.patch.object(
            alw, "scrub", side_effect=lambda t: t.replace("tok", "[redacted]")
        ):
            self.assertEqual(alw.build_excerpt(["tok"]), "[redacted]")


class TestBuildPrompt(unittest.TestCase):
    def test_whole_prompt(self):
        self.assertEqual(
            alw.build_prompt(
                "USER: fais le fix", "2026-09-23", "claude-code", "Perrine"
            ),
            """You write one entry in a cross-agent log — your working directory — for a coding
session that just ended. The log is the async narrative channel between sessions: what a session
did, across chantiers, that no single kanban card's handoff would hold — decisions, root causes
found, what was left open, gotchas another session would otherwise rediscover.

Decide first. If the excerpt holds nothing another session needs (a routine fix fully told by its
commit, exploration with no conclusion), write nothing and reply `nothing to log`.

Otherwise write exactly one new file `2026-09-23_claude-code-Perrine_<slug>.md`, <slug> being 3-6 kebab-case words naming
the subject. Read today's existing `2026-09-23_claude-code-Perrine*.md` first: never repeat what one already says, never
modify one. First line `2026-09-23 · claude-code (Perrine)`, then `## ` sections, dense prose, file
paths and ids exact. Write in the language of the excerpt's `USER:` lines, whatever language this
instruction is in. Never include secrets, credentials or internal URLs.

Session excerpt — user messages, assistant text, edited paths, commit commands; tool outputs
omitted:
<excerpt>
USER: fais le fix
</excerpt>
""",
        )


class TestToolName(unittest.TestCase):
    def test_segment_before_first_underscore(self):
        with unittest.mock.patch.dict(
            os.environ, {"AI_AGENT": "claude-code_2-1-280_agent"}
        ):
            self.assertEqual(alw.tool_name(), "claude-code")

    def test_absent_is_none(self):
        env = {k: v for k, v in os.environ.items() if k != "AI_AGENT"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(alw.tool_name())


class TestMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "_KANBAN").mkdir()
        self.log_dir = root / "_KANBAN" / "_AGENT_LOG"
        self.log_dir.mkdir()
        self.cwd = root / "repo"
        self.cwd.mkdir()
        self.transcript = root / "t.jsonl"
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in (alw.GUARD_ENV, "CLAUDE_PROJECT_DIR")
        }
        env["AI_AGENT"] = "claude-code_2-1-280_agent"
        patcher = unittest.mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        identity = unittest.mock.patch.object(
            alw, "git_identity", return_value="Perrine"
        )
        identity.start()
        self.addCleanup(identity.stop)
        launcher = unittest.mock.patch.object(alw, "launch_writer", return_value=True)
        self.launch_writer = launcher.start()
        self.addCleanup(launcher.stop)

    def run_main(self, *entries):
        _write_transcript(self.transcript, list(entries))
        event = {"transcript_path": str(self.transcript), "cwd": str(self.cwd)}
        with unittest.mock.patch("sys.stdin", io.StringIO(json.dumps(event))):
            alw.main()

    def test_work_without_log_launches_writer_in_agent_log(self):
        self.run_main(_assistant(_tool("Bash", command="git commit -m x")))
        self.launch_writer.assert_called_once()
        log_dir, prompt = self.launch_writer.call_args.args
        self.assertEqual(log_dir, self.log_dir)
        self.assertIn("_claude-code-Perrine_<slug>.md", prompt)

    def test_log_already_written_launches_nothing(self):
        self.run_main(
            _assistant(_tool("Bash", command="git commit -m x")),
            _assistant(_tool("Write", file_path=str(self.log_dir / "2026-09-23_x.md"))),
        )
        self.launch_writer.assert_not_called()

    def test_log_only_write_launches_nothing(self):
        """A write under the now-nested `_AGENT_LOG/` also matches `_KANBAN` (`did_work` True),
        but `wrote_log` alone still short-circuits `main()` — no card/board edit at all here."""
        self.run_main(
            _assistant(_tool("Write", file_path=str(self.log_dir / "2026-09-26_x.md")))
        )
        self.launch_writer.assert_not_called()

    def test_no_work_launches_nothing(self):
        self.run_main(_user("juste une question"))
        self.launch_writer.assert_not_called()

    def test_guard_env_stops_recursion(self):
        os.environ[alw.GUARD_ENV] = "1"
        self.run_main(_assistant(_tool("Bash", command="git commit -m x")))
        self.launch_writer.assert_not_called()

    def test_no_tool_name_launches_nothing(self):
        del os.environ["AI_AGENT"]
        self.run_main(_assistant(_tool("Bash", command="git commit -m x")))
        self.launch_writer.assert_not_called()


class TestLaunchWriter(unittest.TestCase):
    def test_detached_restricted_haiku_with_guard_env(self):
        with (
            tempfile.TemporaryDirectory() as d,
            unittest.mock.patch.object(alw.shutil, "which", return_value="/bin/claude"),
            unittest.mock.patch.object(alw.subprocess, "Popen") as popen,
        ):
            self.assertTrue(alw.launch_writer(Path(d), "prompt"))
        cmd = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual(cmd[:4], ["/bin/claude", "-p", "--model", "haiku"])
        self.assertIn("--restricted", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Glob,Write")
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["env"][alw.GUARD_ENV], "1")
        self.assertEqual(kwargs["cwd"], Path(d))

    def test_no_claude_binary(self):
        with unittest.mock.patch.object(alw.shutil, "which", return_value=None):
            self.assertFalse(alw.launch_writer(Path("."), "prompt"))


if __name__ == "__main__":
    unittest.main()
