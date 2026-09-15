#!/usr/bin/env python3
"""Tests for plugins/pc-autodiscovery/hooks/clear_pending_queue.py — run with:
python3 -m unittest discover tests

Defines "done": a UserPromptSubmit empties the pending_user_action queue *only
when pending_gate has latched a deny* — so a `reload-plugins` queued at
SessionStart (before the gate runs) survives the first prompt and its nudge
surfaces, while a peeked deny key stops denying once the user
sends their next message. Fails open.
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

import clear_pending_queue as clearer  # noqa: E402
import pending_gate as gate  # noqa: E402
import pending_user_action as pua  # noqa: E402


class TestClearPendingQueue(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = pua.STATE_DIR
        pua.STATE_DIR = Path(self._tmp.name)
        self.session = "sess-clear"

    def tearDown(self):
        pua.STATE_DIR = self._orig
        self._tmp.cleanup()

    def _main(self, stdin_text: str) -> str:
        out = io.StringIO()
        sys.stdin = io.StringIO(stdin_text)
        try:
            with redirect_stdout(out):
                clearer.main()
        finally:
            sys.stdin = sys.__stdin__
        return out.getvalue().strip()

    def _prompt(self) -> None:
        self.assertEqual(self._main(json.dumps({"session_id": self.session})), "{}")

    def test_a_latched_deny_is_cleared_by_the_next_prompt(self):
        pua.require(self.session, "goal", "/goal X")
        gate.latch(self.session)
        self._prompt()
        self.assertEqual(pua.pending(self.session), {})
        self.assertFalse(gate.is_latched(self.session))

    def test_an_unlatched_queue_survives_the_prompt(self):
        # SessionStart queued reload-plugins; the gate has not run, so no latch.
        pua.require(self.session, "reload-plugins", "/reload-plugins")
        self._prompt()
        self.assertEqual(
            pua.pending(self.session), {"reload-plugins": "/reload-plugins"}
        )

    def test_empty_queue_stays_empty(self):
        self._prompt()
        self.assertEqual(pua.pending(self.session), {})

    def test_fails_open_on_a_broken_payload(self):
        self.assertEqual(self._main("not json"), "{}")


if __name__ == "__main__":
    unittest.main()
