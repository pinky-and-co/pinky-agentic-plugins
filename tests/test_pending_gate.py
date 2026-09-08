#!/usr/bin/env python3
"""Tests for plugins/pc-core/hooks/pending_gate.py — run with:
python3 -m unittest discover tests

Defines "done": with an empty queue the gate is transparent (`{}`); with
anything queued it returns a PreToolUse `ask` carrying every message and
empties the queue, so the request is a hard stop that surfaces exactly once.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HOOKS = Path(__file__).parent.parent / "plugins" / "pc-core" / "hooks"
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

    def test_a_queued_action_becomes_a_permission_ask(self):
        pua.require(self.session, "reload-plugins", "/reload-plugins")
        result = self._run()
        hso = result["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "ask")
        self.assertIn("/reload-plugins", hso["permissionDecisionReason"])

    def test_every_queued_message_is_in_the_reason(self):
        pua.require(self.session, "rename", "/rename slug")
        pua.require(self.session, "goal", "/goal Fini = X")
        reason = self._run()["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("/rename slug", reason)
        self.assertIn("/goal Fini = X", reason)

    def test_the_queue_is_emptied_so_it_surfaces_once(self):
        pua.require(self.session, "k", "msg")
        self._run()
        self.assertEqual(pua.pending(self.session), {})
        self.assertEqual(self._run(), {})


if __name__ == "__main__":
    unittest.main()
