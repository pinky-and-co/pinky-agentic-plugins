#!/usr/bin/env python3
"""End-to-end coverage for the `pending_user_action` channel `chantier/SKILL.md` wires into —
run with: python3 -m unittest discover tests

Unlike test_pending_gate.py / test_clear_pending_queue.py (which import the hook modules
in-process), these tests spawn the real scripts as subprocesses, HOME-isolated per test: the
vendored CLI `chantier` itself calls (`pc-kanban/skills/chantier/scripts/
pc_kanban_pending_user_action.py`) and the two `pc-autodiscovery` hooks it feeds
(`pending_gate.py`, `clear_pending_queue.py`). Only a real subprocess proves the two plugins
actually agree on `~/.claude/pinky/plugin-scope/<session>.pending.json` — an in-process import
of both modules would silently share Python state that two independent hook invocations never do.

The oracle is the reproduced sequence: `chantier` queues `goal`/`rename` with `surfaced=True` and
then ends its turn without a further tool call (its own rule), so no `deny` ever latches this turn.
The `surfaced` flag is what resolves them anyway, keeping the next turn's first — unrelated — tool
call from being wrongly denied: the queue must already be empty by the time that call happens, not
waiting on a latch that this turn never set.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
KANBAN_CLI = (
    REPO
    / "plugins"
    / "pc-kanban"
    / "skills"
    / "chantier"
    / "scripts"
    / "pc_kanban_pending_user_action.py"
)
GATE = REPO / "plugins" / "pc-autodiscovery" / "hooks" / "pending_gate.py"
CLEARER = REPO / "plugins" / "pc-autodiscovery" / "hooks" / "clear_pending_queue.py"
SKILL = REPO / "plugins" / "pc-kanban" / "skills" / "chantier" / "SKILL.md"
FINISHING = (
    REPO
    / "plugins"
    / "pc-kanban"
    / "skills"
    / "kanban"
    / "references"
    / "finishing-a-card.md"
)


class PendingActionChannelCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.session = "sess-e2e"
        self.env = {
            "HOME": self._tmp.name,
            "PATH": os.environ.get("PATH", ""),
            "CLAUDE_CODE_SESSION_ID": self.session,
        }

    def tearDown(self):
        self._tmp.cleanup()

    def _require_via_skill_cli(
        self, key: str, message: str, surfaced: bool = False
    ) -> None:
        """What a chantier SKILL.md bullet's own Bash call does."""
        argv = [sys.executable, str(KANBAN_CLI), "require", key, message]
        if surfaced:
            argv.append("--surfaced")
        result = subprocess.run(argv, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def _user_prompt_submit(self) -> dict:
        result = subprocess.run(
            [sys.executable, str(CLEARER)],
            input=json.dumps({"session_id": self.session}),
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def _pre_tool_use(self) -> dict:
        result = subprocess.run(
            [sys.executable, str(GATE)],
            input=json.dumps({"session_id": self.session}),
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_goal_queued_by_the_vendored_cli_never_locks_the_tools(self):
        self._require_via_skill_cli("goal", "/goal done = X", surfaced=True)
        hso = self._pre_tool_use().get("hookSpecificOutput", {})
        self.assertNotEqual(hso.get("permissionDecision"), "deny")

    def test_repro_a_clean_stop_after_goal_does_not_wrongly_deny_the_next_turn(self):
        # chantier arrête son tour juste après avoir mis "goal" en file, --surfaced — aucun
        # tool call, donc aucun `deny`, donc aucun latch. Le prochain message user (collant
        # /goal) déclenche un UserPromptSubmit.
        self._require_via_skill_cli("goal", "/goal done = X", surfaced=True)
        self._user_prompt_submit()
        # Un tool call sans rapport ouvre le tour suivant.
        self.assertEqual(self._pre_tool_use(), {})

    def test_repro_the_same_for_rename(self):
        self._require_via_skill_cli("rename", "/rename my-card", surfaced=True)
        self._user_prompt_submit()
        self.assertEqual(self._pre_tool_use(), {})

    def test_repro_the_combined_goal_bullet_call_queuing_both_keys(self):
        # Ce que fait l'appel Bash du bullet Goal : rename et goal, --surfaced, dans le même lot.
        self._require_via_skill_cli("rename", "/rename my-card", surfaced=True)
        self._require_via_skill_cli("goal", "/goal done = X", surfaced=True)
        self._user_prompt_submit()
        self.assertEqual(self._pre_tool_use(), {})

    def test_reload_plugins_still_needs_the_latch_round_trip(self):
        # Contrairement à goal/rename, reload-plugins peut être mis en file silencieusement au
        # SessionStart, avant que le gate n'ait tourné — il doit survivre à un prompt sans latch
        # pour que sa relance surface encore une fois.
        self._require_via_skill_cli("reload-plugins", "/reload-plugins")
        self._user_prompt_submit()
        hso = self._pre_tool_use()["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("/reload-plugins", hso["permissionDecisionReason"])


_CLI = (
    'python3 "${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/'
    'pc_kanban_pending_user_action.py"'
)


class ChantierSkillWiringCase(unittest.TestCase):
    """Pins the SKILL.md wiring so a later edit cannot silently drop a require() call.

    Each assertion is scoped to the one bullet/branch it claims to cover — a bare substring
    shared by two branches (the Grill-gate standalone call and the Goal bullet's combined
    call are otherwise identical up to `--surfaced`) would stay green even if one of the two
    calls it claims to pin were deleted.
    """

    def setUp(self):
        self.text = SKILL.read_text()

    def _section(self, start: str, end: str) -> str:
        i = self.text.index(start)
        j = self.text.index(end, i)
        return self.text[i:j]

    def test_allowed_tools_permits_the_vendored_cli(self):
        allowed_tools_line = next(
            line for line in self.text.splitlines() if line.startswith("allowed-tools:")
        )
        self.assertIn(
            "Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/"
            "pc_kanban_pending_user_action.py:*)",
            allowed_tools_line,
        )

    def test_take_path_never_queues_rename(self):
        self.assertNotIn("require rename", self.text)
        self.assertNotIn("**Session name.**", self.text)

    def test_the_skill_never_queues_a_goal(self):
        self.assertNotIn("require goal", self.text)
        self.assertNotIn("--surfaced", self.text)

    def test_review_clause_asks_only_a_review_with_its_findings_handled(self):
        section = self._section("**Review clause.**", "**Waiver clause.**")
        flat = " ".join(section.split())
        self.assertIn(
            "the production-code diff has been reviewed in a fresh context, never by this session itself;"
            " documentation, docstrings, comments, tests and edits applying a reviewer finding as written"
            " never reopen a review; each finding fixed, refuted in the transcript, or noted in the card's"
            " `## Decisions`",
            flat,
        )
        self.assertNotIn("reviewed through the `adversarial` tool", flat)
        self.assertIn("never in the condition", flat)
        self.assertNotIn("relayed verbatim", flat)
        self.assertNotIn("third round", flat)
        self.assertIn("never a new card", flat)
        self.assertNotIn("follow-up card", flat)
        self.assertNotIn("when the first line", section)
        self.assertNotIn("box checked on the card`", flat)

    def test_the_waiver_clause_covers_only_a_clause_false_by_construction(self):
        self.assertNotIn("**Escape clause.**", self.text)
        section = self._section("**Waiver clause.**", "**Amending the condition.**")
        flat = " ".join(section.split())
        self.assertIn("shown false by construction", flat)
        self.assertIn("waived that clause or amended it", flat)

    def test_an_amendment_needs_no_line_to_paste(self):
        section = self._section("**Amending the condition.**", "8. Execute")
        flat = " ".join(section.split())
        self.assertIn("the exact text of the amended clause", flat)
        self.assertIn("no line to paste", flat)
        self.assertIn("`tmux`", flat)
        self.assertIn("never from `installPath`", flat)
        self.assertIn("Never propose `/goal clear`", flat)
        self.assertNotIn("for the user to paste", flat)
        self.assertNotIn("require goal", flat)


class FinishingCardRenameCase(unittest.TestCase):
    """Pins the close-time rename in finishing-a-card.md."""

    def setUp(self):
        text = FINISHING.read_text()
        start = text.index("### Naming the session, at the end of the session only")
        self.section = text[start : text.index("### Relaying a multi-repo card")]

    def test_queues_rename_surfaced(self):
        self.assertIn(
            f"{_CLI} require rename '/rename <name>' --surfaced", self.section
        )

    def test_covers_every_naming_case_for_any_session(self):
        for needle in (
            "Any session, worker or orchestrator",
            "Never per card",
            "`≥40` %",
            "The user ends the session",
            "One card closed",
            "same `sert` root",
            "+N",
            "cannot be reconstructed",
        ):
            self.assertIn(needle, self.section)


if __name__ == "__main__":
    unittest.main()


KANBAN_PROACTIVITY = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-kanban"
    / "hooks"
    / "kanban-proactivity"
    / "kanban-proactivity.md"
)


class SessionEndRenameCase(unittest.TestCase):
    """The user ending the session triggers the /rename proposal."""

    def test_both_files_carry_the_instruction(self):
        self.assertIn("The user ends the session", FINISHING.read_text())
        self.assertIn(
            "When the user ends the session,\n  propose the `/rename`",
            KANBAN_PROACTIVITY.read_text(),
        )


_NOT_ANSWERED = (
    "stating why `adversarial` did not answer when the `Method:` "
    "is `reviewer` on a change that has a diff"
)


class ReviewHonestyPinCase(unittest.TestCase):
    """The same sentence is pinned in every artefact that carries it."""

    def test_finishing_relay_rule_pins_it(self):
        text = " ".join(FINISHING.read_text().split())
        self.assertIn(_NOT_ANSWERED, text)

    def test_reviewer_report_format_pins_it(self):
        reviewer = REPO / "plugins" / "pc-core" / "agents" / "reviewer.md"
        text = " ".join(reviewer.read_text().split())
        self.assertIn(
            "a `Method: reviewer` report states why `adversarial` did not answer", text
        )
