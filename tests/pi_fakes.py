"""Test doubles for the `pi --mode json` process `model_adversarial.call_cli` spawns.

Only the process boundary is faked: `call_cli` runs for real on the events a `PiPopen` hands it,
so its reading of the stream, its failure rule and its timeout handling are what the tests check.
"""

from __future__ import annotations

import io
import json
import subprocess
import unittest.mock
from typing import Any


def pi_events(
    text: str = "",
    stop_reason: str = "stop",
    error_message: str | None = None,
) -> str:
    """The JSONL a `pi --mode json` run prints for one assistant answer.

    Args:
        text: the answer's text block.
        stop_reason: the final assistant message's `stopReason`.
        error_message: its `errorMessage`, when the provider failed.

    Returns:
        The events, one per line, newline-terminated.
    """
    message: dict[str, Any] = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}] if text else [],
        "stopReason": stop_reason,
    }
    if error_message is not None:
        message["errorMessage"] = error_message
    events = [
        {"type": "session", "id": "00000000-0000-4000-8000-000000000000"},
        {"type": "agent_start"},
        {"type": "message_end", "message": message},
        {"type": "agent_end"},
        {"type": "agent_settled", "aborted": False},
    ]
    return "".join(json.dumps(e) + "\n" for e in events)


class FakePiProcess:
    """What `Popen` returns: a finished process with canned streams."""

    def __init__(
        self,
        stdout: str,
        stderr: str,
        returncode: int,
        wait_error: subprocess.TimeoutExpired | None,
        wait_timeouts: list[float | None],
    ) -> None:
        """Hold the canned run.

        Args:
            stdout: what the process printed.
            stderr: what it wrote to its error stream.
            returncode: its exit status.
            wait_error: raised by the first `wait` only, to play a run past its timeout.
            wait_timeouts: the list `wait` appends the timeout it was given to.
        """
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.args = "pi"
        self.pid = 0  # never a real group: `call_cli` signals only a positive pid
        self._wait_error = wait_error
        self._wait_timeouts = wait_timeouts

    def wait(self, timeout: float | None = None) -> int:
        """Record `timeout`, then return the exit status or play the canned timeout."""
        self._wait_timeouts.append(timeout)
        error, self._wait_error = self._wait_error, None
        if error is not None:
            raise error
        return self.returncode

    def kill(self) -> None:
        """Nothing to kill."""


class PiPopen(unittest.mock.MagicMock):
    """A `Popen` replacement configured like the `subprocess.run` mocks the suite used to patch.

    Its `return_value` / `side_effect` items are the outcome of one `pi` call: an object with
    `stdout`, `stderr` and `returncode` (a `CompletedProcess`, a `MagicMock`) — its stdout is the
    answer text, rendered as the events of a successful run — a `CalledProcessError` (a process
    that failed), a `TimeoutExpired` (a run past its timeout, raised by `wait`), or any other
    exception (raised when spawning). `wait_timeouts` lists the timeout of each `wait`.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Start with no `wait` seen."""
        super().__init__(*args, **kwargs)
        self.wait_timeouts: list[float | None] = []

    def _get_child_mock(self, **kwargs: Any) -> unittest.mock.MagicMock:
        """Children are plain mocks, not another process factory."""
        return unittest.mock.MagicMock(**kwargs)

    def _execute_mock_call(self, *args: Any, **kwargs: Any) -> FakePiProcess:
        """Turn the configured outcome into a finished process."""
        try:
            outcome = super()._execute_mock_call(*args, **kwargs)
        except subprocess.TimeoutExpired as e:
            return FakePiProcess("", "", 0, e, self.wait_timeouts)
        except subprocess.CalledProcessError as e:
            return FakePiProcess(
                e.output or "",
                e.stderr or "",
                e.returncode,
                None,
                self.wait_timeouts,
            )
        stdout = outcome.stdout or ""
        if outcome.returncode == 0:
            stdout = pi_events(stdout)
        return FakePiProcess(
            stdout,
            outcome.stderr or "",
            outcome.returncode,
            None,
            self.wait_timeouts,
        )
