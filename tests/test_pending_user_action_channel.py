#!/usr/bin/env python3
"""End-to-end coverage for the `pending_user_action` channel `chantier/SKILL.md` wires into —
run with: python3 -m unittest discover tests

Unlike test_pending_gate.py / test_clear_pending_queue.py (which import the hook modules
in-process), these tests spawn the real scripts as subprocesses, HOME-isolated per test: the
vendored CLI `chantier` itself calls (`pc-kanban/skills/chantier/scripts/
pc_kanban_pending_user_action.py`) and the two `pc-autodiscovery` hooks it feeds
(`pending_gate.py`, `clear_pending_queue.py`). Only a real subprocess proves the two plugins
actually agree on `~/.claude/pinky-plugin-scope/<session>.pending.json` — an in-process import
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

    def test_goal_queued_by_the_vendored_cli_reaches_the_autodiscovery_gate(self):
        self._require_via_skill_cli("goal", "/goal done = X", surfaced=True)
        hso = self._pre_tool_use()["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("/goal done = X", hso["permissionDecisionReason"])

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

    def test_grill_gate_path_queues_rename_alone_surfaced(self):
        section = self._section("**Grill gate — before the goal.**", "- **Goal.**")
        self.assertIn(
            f"{_CLI} require rename '/rename <card-slug>' --surfaced", section
        )
        self.assertNotIn("require goal", section)

    def test_orphan_sert_path_queues_rename_alone_surfaced(self):
        section = self._section(
            "**Orphan `sert`.**", "`/goal` is parsed at the CLI level"
        )
        self.assertIn(
            f"{_CLI} require rename '/rename <card-slug>' --surfaced", section
        )
        self.assertNotIn("require goal", section)

    def test_goal_path_queues_both_keys_surfaced_in_one_call(self):
        section = self._section("Once `<condition>` is fully resolved", "8. Execute")
        self.assertIn(
            f"{_CLI} require rename '/rename <card-slug>' --surfaced && "
            f"{_CLI} require goal '/goal <condition>' --surfaced",
            section,
        )


if __name__ == "__main__":
    unittest.main()
