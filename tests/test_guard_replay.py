"""Tests for plugins/pc-kanban/skills/permission-change/scripts/guard_replay.py."""

import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent
        / "plugins"
        / "pc-kanban"
        / "skills"
        / "permission-change"
        / "scripts"
    ),
)
sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent / "plugins" / "pc-kanban" / "hooks" / "guard-state"
    ),
)

import guard_replay as gr


def _line(command: str, cwd: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "cwd": cwd,
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": command}}
                ]
            },
        }
    )


class ReplayTests(unittest.TestCase):
    def test_counts_main_and_subagent_apart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = str(root / "proj")
            (root / "p" / "s1" / "subagents").mkdir(parents=True)
            (root / "p" / "s1.jsonl").write_text(
                "\n".join(
                    [
                        _line("cd '../outside' && echo x > f", cwd),
                        _line("cd '../outside' && echo x > f", cwd),  # duplicate
                        _line("cd 'sub' ; ls", cwd),
                        _line("cd ../outside && echo x > f; dd if=a of=../out/f", cwd),
                        _line('cd "$REPO" && echo x > f', cwd),
                        _line("ls", cwd),
                    ]
                ),
                encoding="utf-8",
            )
            (root / "p" / "s1" / "subagents" / "agent-1.jsonl").write_text(
                _line("git -C '../outside' commit -am x", cwd), encoding="utf-8"
            )
            result = gr.replay_transcripts(root, gr.load_tree_guard())
        self.assertEqual(result["unique"], {"main": 5, "subagent": 1})
        cd = result["main"]["cd-quoted"]
        self.assertEqual((cd["total"], cd["outside"]), (2, 1))
        dd = result["main"]["dd-of"]
        self.assertEqual((dd["total"], dd["outside"], dd["asked"]), (1, 1, 1))
        var = result["main"]["quoted-var-target"]
        self.assertEqual((var["total"], var["outside"], var["asked"]), (1, 0, 0))
        git_c = result["subagent"]["git-C-quoted"]
        self.assertEqual((git_c["total"], git_c["outside"]), (1, 1))
        self.assertNotIn("git-C-quoted", result["main"])

    def test_example_is_one_line_of_at_most_80_characters(self) -> None:
        command = "\n".join(["echo " + "x" * 40] * 12)[:500]
        self.assertEqual(len(command), 500)
        shown = gr.one_line(command)
        self.assertNotIn("\n", shown)
        self.assertLessEqual(len(shown), 80)

    def test_report_examples_are_truncated(self) -> None:
        long = "cd '../o' && " + "\n".join(["echo y > f"] * 60)
        result = {
            "unique": {"main": 1, "subagent": 0},
            "main": {
                "cd-quoted": {"total": 1, "outside": 1, "asked": 0, "examples": [long]}
            },
            "subagent": {},
        }
        for line in gr.format_report(result, 3).splitlines():
            if line.startswith("    "):
                self.assertLessEqual(len(line.strip()), 80)


class DiffTests(unittest.TestCase):
    def test_tree_against_itself_changes_nothing(self) -> None:
        self.assertEqual(
            gr.diff_guards(gr.load_tree_guard("a"), gr.load_tree_guard("b")), []
        )

    def test_changed_verdict_is_reported_with_its_family(self) -> None:
        class Always:
            @staticmethod
            def bash_write_escapes(*_args: object) -> list[str]:
                return ["x"]

        changed = gr.diff_guards(Always(), gr.load_tree_guard())  # type: ignore[arg-type]
        self.assertTrue(changed)
        self.assertTrue(
            {"bare-target", "write-redirect"} <= {c["family"] for c in changed}
        )

    def test_main_exit_code_follows_allow_family(self) -> None:
        class Always:
            @staticmethod
            def bash_write_escapes(*_args: object) -> list[str]:
                return ["x"]

        families = {c["family"] for c in gr.diff_guards(Always(), gr.load_tree_guard())}  # type: ignore[arg-type]
        allow = [
            arg for family in sorted(families) for arg in ("--allow-family", family)
        ]
        with unittest.mock.patch.object(gr, "load_ref_guard", lambda _ref: Always()):
            self.assertEqual(gr.main(["diff", "--base", "any"]), 1)
            self.assertEqual(gr.main(["diff", "--base", "any", *allow]), 0)
        with unittest.mock.patch.object(
            gr, "load_ref_guard", lambda _ref: gr.load_tree_guard()
        ):
            self.assertEqual(gr.main(["diff", "--base", "any"]), 0)


if __name__ == "__main__":
    unittest.main()
