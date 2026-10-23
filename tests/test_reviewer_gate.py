#!/usr/bin/env python3
"""Tests for plugins/pc-models/hooks/reviewer_gate.py — run with:
python3 -m unittest discover tests
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parent.parent / "plugins" / "pc-models" / "hooks")
)

import reviewer_gate as rg


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _result_line(content: object) -> str:
    return json.dumps(
        {
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t", "content": content}
                ]
            }
        }
    )


def _notification_line(result: str) -> str:
    """A background MCP task's completion as the transcript records it."""
    return json.dumps(
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": (
                "<task-notification>\n<task-id>k0hy4d7j3</task-id>\n"
                "<status>completed</status>\n"
                f"<result>\n{json.dumps({'result': result})}\n</result>\n"
                "</task-notification>"
            ),
        }
    )


class TestReviewerGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "a.txt").write_text("a\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.transcript = Path(self._tmp.name) / "t.jsonl"

    def _payload(self, subagent_type="reviewer"):
        return {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": subagent_type},
            "cwd": str(self.repo),
            "transcript_path": str(self.transcript),
        }

    def _dirty(self):
        (self.repo / "a.txt").write_text("changed\n")

    def _decision(self, payload):
        out = rg.decide(payload)
        return out["hookSpecificOutput"]["permissionDecision"] if out else None

    def test_denies_reviewer_on_diff_without_adversarial_failure(self):
        self._dirty()
        self.transcript.write_text("")
        self.assertEqual(self._decision(self._payload()), "deny")

    def test_denies_namespaced_reviewer(self):
        self._dirty()
        self.assertEqual(self._decision(self._payload("pc-core:reviewer")), "deny")

    def test_allows_other_subagent_type(self):
        self._dirty()
        self.assertIsNone(self._decision(self._payload("general-purpose")))

    def test_allows_without_diff(self):
        self.assertIsNone(self._decision(self._payload()))

    def test_allows_after_wrapped_blocked(self):
        self._dirty()
        self.transcript.write_text(
            _result_line('{"result":"[adversarial] BLOCKED — every candidate failed."}')
        )
        self.assertIsNone(self._decision(self._payload()))

    def test_allows_after_bare_error_in_text_blocks(self):
        self._dirty()
        self.transcript.write_text(
            _result_line(
                [{"type": "text", "text": "[adversarial] ERROR — cannot diff"}]
            )
        )
        self.assertIsNone(self._decision(self._payload()))

    def test_denies_after_successful_adversarial(self):
        self._dirty()
        self.transcript.write_text(
            _result_line('{"result":"[answered by x · sonnet]\\nverdict"}')
        )
        self.assertEqual(self._decision(self._payload()), "deny")

    def test_allows_after_error_delivered_by_a_background_task_notification(self):
        self._dirty()
        self.transcript.write_text(
            _notification_line("[adversarial] ERROR — wall-clock cap (1800s) reached.")
        )
        self.assertIsNone(self._decision(self._payload()))

    def test_denies_after_successful_background_task_notification(self):
        self._dirty()
        self.transcript.write_text(
            _notification_line("[answered by x · sonnet]\nverdict")
        )
        self.assertEqual(self._decision(self._payload()), "deny")

    def test_queued_user_prompt_quoting_the_prefix_does_not_count(self):
        self._dirty()
        self.transcript.write_text(
            json.dumps(
                {
                    "type": "queue-operation",
                    "operation": "enqueue",
                    "content": '<result>{"result":"[adversarial] ERROR"}</result>',
                }
            )
        )
        self.assertEqual(self._decision(self._payload()), "deny")

    def test_notification_result_not_opening_with_the_prefix_does_not_count(self):
        self._dirty()
        self.transcript.write_text(
            _notification_line("verdict: the log said [adversarial] ERROR once")
        )
        self.assertEqual(self._decision(self._payload()), "deny")

    def test_malformed_notification_and_non_dict_lines_deny(self):
        self._dirty()
        unclosed = json.dumps(
            {
                "type": "queue-operation",
                "content": "<task-notification><result>[adversarial] ERROR",
            }
        )
        self.transcript.write_text(f'{unclosed}\n["[adversarial] ERROR"]\n')
        self.assertEqual(self._decision(self._payload()), "deny")

    def test_blocked_text_outside_tool_result_does_not_count(self):
        self._dirty()
        self.transcript.write_text(
            json.dumps({"message": {"content": "[adversarial] BLOCKED"}})
        )
        self.assertEqual(self._decision(self._payload()), "deny")

    def test_untracked_only_change_is_denied(self):
        (self.repo / "new.txt").write_text("n\n")
        self.assertEqual(self._decision(self._payload()), "deny")

    def test_a_file_named_like_the_base_branch_does_not_disable_the_gate(self):
        (self.repo / "main").write_text("a file, not the branch\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "file named main")
        self._dirty()
        self.assertEqual(self._decision(self._payload()), "deny")

    def test_ignored_untracked_file_is_no_diff(self):
        (self.repo / ".gitignore").write_text("ignored.txt\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "ignore")
        (self.repo / "ignored.txt").write_text("x\n")
        self.assertIsNone(self._decision(self._payload()))

    def test_default_branch_master_is_honoured(self):
        _git(self.repo, "branch", "-m", "main", "master")
        self._dirty()
        self.assertEqual(self._decision(self._payload()), "deny")
        _git(self.repo, "checkout", "-q", "-b", "feat")
        _git(self.repo, "commit", "-qam", "work")
        self.assertEqual(self._decision(self._payload()), "deny")
        _git(self.repo, "checkout", "-q", "master")
        self.assertIsNone(self._decision(self._payload()))

    def test_origin_head_resolves_the_base(self):
        origin = Path(self._tmp.name) / "origin.git"
        _git(self.repo, "branch", "-m", "main", "trunk")
        _git(self.repo, "clone", "-q", "--bare", str(self.repo), str(origin))
        clone = Path(self._tmp.name) / "clone"
        _git(self.repo, "clone", "-q", str(origin), str(clone))
        _git(clone, "config", "user.email", "t@t")
        _git(clone, "config", "user.name", "t")
        _git(clone, "branch", "-m", "trunk", "local-only-name")
        self.assertEqual(rg.resolve_base(str(clone)), "origin/trunk")
        (clone / "a.txt").write_text("changed\n")
        payload = self._payload()
        payload["cwd"] = str(clone)
        self.assertEqual(self._decision(payload), "deny")
        _git(clone, "checkout", "-q", "a.txt")
        self.assertIsNone(self._decision(payload))

    def test_unresolvable_base_allows(self):
        _git(self.repo, "branch", "-m", "main", "develop")
        self._dirty()
        (self.repo / "new.txt").write_text("n\n")
        self.assertIsNone(rg.resolve_base(str(self.repo)))
        self.assertIsNone(self._decision(self._payload()))

    def test_main_fails_open_on_garbage_stdin(self):
        proc = subprocess.run(
            [sys.executable, str(Path(rg.__file__))],
            input="not json",
            capture_output=True,
            text=True,
        )
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
