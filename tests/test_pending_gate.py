#!/usr/bin/env python3
"""Tests for plugins/pc-autodiscovery/hooks/pending_gate.py — run with:
python3 -m unittest discover tests

Defines "done": an empty queue is transparent (`{}`). A queued `reload-plugins`
or `goal` returns a PreToolUse `deny` and leaves the queue intact (peek, not
pop), so every tool call for the rest of the turn is denied. Any other queued
key returns an `ask` and empties the queue, so it surfaces exactly once.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HOOKS = Path(__file__).parent.parent / "plugins" / "pc-autodiscovery" / "hooks"
sys.path.insert(0, str(HOOKS))

import pending_user_action as pua  # noqa: E402
import pending_gate as gate  # noqa: E402


class TestPendingGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = pua.STATE_DIR
        pua.STATE_DIR = Path(self._tmp.name)
        self.session = "sess-gate"

    def tearDown(self):
        pua.STATE_DIR = self._orig
        self._tmp.cleanup()

    def _run(self) -> dict:
        out = io.StringIO()
        with redirect_stdout(out):
            gate.main_with_payload({"session_id": self.session})
        return json.loads(out.getvalue())

    def test_empty_queue_is_transparent(self):
        self.assertEqual(self._run(), {})

    def test_reload_plugins_is_a_deny(self):
        pua.require(self.session, "reload-plugins", "/reload-plugins")
        hso = self._run()["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("/reload-plugins", hso["permissionDecisionReason"])

    def test_goal_is_a_deny(self):
        pua.require(self.session, "goal", "/goal Fini = X")
        self.assertEqual(
            self._run()["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_rename_is_an_ask(self):
        pua.require(self.session, "rename", "/rename slug")
        hso = self._run()["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "ask")
        self.assertIn("/rename slug", hso["permissionDecisionReason"])

    def test_deny_key_wins_over_an_ask_key_in_the_same_batch(self):
        pua.require(self.session, "rename", "/rename slug")
        pua.require(self.session, "goal", "/goal Fini = X")
        hso = self._run()["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("/rename slug", hso["permissionDecisionReason"])
        self.assertIn("/goal Fini = X", hso["permissionDecisionReason"])

    def test_deny_peeks_so_every_call_of_the_turn_is_denied(self):
        pua.require(self.session, "reload-plugins", "/reload-plugins")
        self._run()
        self.assertEqual(
            pua.pending(self.session), {"reload-plugins": "/reload-plugins"}
        )
        self.assertEqual(
            self._run()["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_deny_sets_the_latch_and_ask_does_not(self):
        pua.require(self.session, "goal", "/goal X")
        self._run()
        self.assertTrue(gate.is_latched(self.session))
        pua.clear_all(self.session)
        gate.unlatch(self.session)
        pua.require(self.session, "rename", "/rename slug")
        self._run()
        self.assertFalse(gate.is_latched(self.session))

    def test_empty_queue_does_not_latch(self):
        self._run()
        self.assertFalse(gate.is_latched(self.session))

    def test_ask_pops_so_it_surfaces_once(self):
        pua.require(self.session, "rename", "/rename slug")
        self._run()
        self.assertEqual(pua.pending(self.session), {})
        self.assertEqual(self._run(), {})

    def _main(self, stdin_text: str) -> str:
        out = io.StringIO()
        sys.stdin = io.StringIO(stdin_text)
        try:
            with redirect_stdout(out):
                gate.main()
        finally:
            sys.stdin = sys.__stdin__
        return out.getvalue().strip()

    def test_main_fails_open_on_a_broken_payload(self):
        # matcher-less PreToolUse: any raise here blocks every tool call
        self.assertEqual(self._main("not json"), "{}")

    def test_main_fails_open_when_the_queue_backend_raises(self):
        def boom(_session_id):
            raise RuntimeError("queue backend down")

        orig, gate.pending = gate.pending, boom
        try:
            self.assertEqual(self._main('{"session_id": "x"}'), "{}")
        finally:
            gate.pending = orig


if __name__ == "__main__":
    unittest.main()
