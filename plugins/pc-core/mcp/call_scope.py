"""The `pi` processes one MCP tool call launched, so that call can take them all down.

A tool call owns a `CallScope`. Every `pi` the call starts — from the tool's own thread or from a
pool of worker threads it fans out to — is adopted by the scope that is current in that thread.
Cancelling the call, or closing the client's channel, kills the whole process group of each of
them and refuses any new one: nothing keeps running for a call nobody waits for. A server killed
by `SIGKILL` cannot do that itself, so each adopted `pi` also gets a guardian (`pi_guardian.py`)
that kills its group once the server is gone.

A client that gives up on a call without closing the channel sends the server nothing, so the
server also bounds every served call by a wall-clock cap (`call_timeout`) that ends it the same
way. Each `pi` a cancelled scope kills is reported on stderr: call id, pid, reason — no other
value ever reaches that log.

Outside a tool call (the CLIs, the tests) no scope is current and every function here does
nothing, so a standalone run behaves as if this module did not exist.
"""

from __future__ import annotations

import contextvars
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from subprocess import Popen
from typing import Any, TypeVar

_T = TypeVar("_T")

_GUARDIAN = Path(__file__).with_name("pi_guardian.py")

#: Wall-clock cap of a served tool call, in seconds, when `PC_CORE_CALL_TIMEOUT_S` says nothing
#: usable — aligned with the client's own idle timeout, so a call the cap ends is a call the
#: client had already given up on.
DEFAULT_CALL_TIMEOUT_S = 1800.0


def call_timeout() -> float:
    """The wall-clock cap a served tool call runs under, in seconds.

    Read from `PC_CORE_CALL_TIMEOUT_S` at each call, so a test or a long-running use can set it
    without restarting the server. A value that is missing, unreadable or not positive falls back
    to the default.

    Returns:
        The cap, in seconds.
    """
    try:
        value = float(os.environ.get("PC_CORE_CALL_TIMEOUT_S", ""))
    except ValueError:
        return DEFAULT_CALL_TIMEOUT_S
    return value if value > 0 else DEFAULT_CALL_TIMEOUT_S


def _report_kill(call_id: str, pid: int, reason: str) -> None:
    """One stderr line per `pi` a cancelled scope kills — call id, pid, reason, nothing else.

    Args:
        call_id: the id of the call the `pi` belonged to (empty outside the server).
        pid: the `pi`'s pid.
        reason: why the call ended (`wall-cap`, `cancelled`, `closed-channel`).
    """
    print(
        f"pc-core: call id={call_id} pi killed pid={pid} reason={reason}",
        file=sys.stderr,
        flush=True,
    )


class CallCancelled(Exception):
    """The call that asked for this `pi` was cancelled: start no other, return no answer."""


def kill_session(proc: Popen[str]) -> None:
    """SIGKILL `pi` and, on POSIX where it runs in its own session (`start_new_session`), every
    process it started — a tool's child holding its pipes goes with it.

    Args:
        proc: the `pi` process.
    """
    # Pas de `killpg` hors POSIX : `proc.kill()` reste atteint dans tous les cas. Jamais un pid
    # <= 0 : `killpg(0, ...)` signalerait notre propre groupe.
    killpg = getattr(os, "killpg", None)
    if killpg is not None and proc.pid > 0:
        try:
            killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    proc.kill()


def _guard(proc: Popen[str]) -> None:
    """Start the process that kills `proc`'s group if this server dies without cleaning up.

    Best effort: without a guardian the other triggers (cancellation, closed channel) still hold.
    A thread waits for the guardian, which ends on its own once the `pi` group is gone, so it is
    reaped at once and never lingers as a zombie.

    Args:
        proc: the `pi` process, leader of its own group.
    """
    # Jamais un pid <= 0 : `killpg(0, ...)` viserait le groupe du gardien lui-même.
    if not hasattr(os, "killpg") or proc.pid <= 0:
        return
    try:
        guardian = subprocess.Popen(
            [sys.executable, str(_GUARDIAN), str(os.getpid()), str(proc.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return
    threading.Thread(target=guardian.wait, daemon=True).start()


class CallScope:
    """The live `pi` processes of one tool call, and whether that call was cancelled."""

    def __init__(self, call_id: str = "") -> None:
        """Start with no process and a call still wanted.

        Args:
            call_id: identifies the call in the kill log (empty outside the server).
        """
        self._lock = threading.Lock()
        self._procs: set[Popen[str]] = set()
        self._cancelled = False
        self._reason = "cancelled"
        self.call_id = call_id

    @property
    def cancelled(self) -> bool:
        """Whether `cancel` was called."""
        return self._cancelled

    def adopt(self, proc: Popen[str]) -> None:
        """Track `proc`; when the call was cancelled meanwhile, kill it instead.

        Args:
            proc: a `pi` process just started for this call.

        Raises:
            CallCancelled: the call was cancelled between the last `check` and now — `proc` is
                killed, reaped and its pipes closed first.
        """
        with self._lock:
            adopted = not self._cancelled
            if adopted:
                self._procs.add(proc)
        if adopted:
            _guard(proc)
            return
        kill_session(proc)
        _report_kill(self.call_id, proc.pid, self._reason)
        proc.wait()
        # L'appelant ne lit jamais ces pipes : il n'arrive pas jusqu'à ses threads de lecture.
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        raise CallCancelled

    def release(self, proc: Popen[str]) -> None:
        """Stop tracking `proc`, which has ended.

        Args:
            proc: a process `adopt` accepted.
        """
        with self._lock:
            self._procs.discard(proc)

    def cancel(self, reason: str = "cancelled") -> None:
        """Refuse any later `pi` and kill every one still running, reporting each kill. Idempotent.

        Args:
            reason: why the call ended — the motif the kill log carries for this scope.
        """
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._reason = reason
            procs = list(self._procs)
        for proc in procs:
            kill_session(proc)
            _report_kill(self.call_id, proc.pid, reason)


_current: contextvars.ContextVar[CallScope | None] = contextvars.ContextVar(
    "call_scope", default=None
)


def run_in(scope: CallScope, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Call `fn` with `scope` as the current scope of this thread.

    Both parameters are positional-only: a tool may have a keyword named `scope`.

    Args:
        scope: the scope the processes `fn` starts belong to.
        fn: the callable to run.
        *args: its positional arguments.
        **kwargs: its keyword arguments.

    Returns:
        What `fn` returns.
    """
    previous = _current.set(scope)
    try:
        return fn(*args, **kwargs)
    finally:
        _current.reset(previous)


def bind(fn: Callable[..., _T]) -> Callable[..., _T]:
    """`fn`, set to run under the scope current NOW — for a worker thread, which has none.

    Args:
        fn: the callable a thread pool will run.

    Returns:
        A callable that runs `fn` under the scope this thread has; `fn` itself when it has none.
    """
    scope = _current.get()
    if scope is None:
        return fn

    def bound(*args: Any, **kwargs: Any) -> _T:
        """Run `fn` under the captured scope."""
        return run_in(scope, fn, *args, **kwargs)

    return bound


def check() -> None:
    """Refuse to go on when the current call was cancelled.

    Raises:
        CallCancelled: the call was cancelled — start no `pi`.
    """
    scope = _current.get()
    if scope is not None and scope.cancelled:
        raise CallCancelled


def adopt(proc: Popen[str]) -> None:
    """Track `proc` in the current scope, if there is one.

    Args:
        proc: a `pi` process just started.

    Raises:
        CallCancelled: see `CallScope.adopt`.
    """
    scope = _current.get()
    if scope is not None:
        scope.adopt(proc)


def release(proc: Popen[str]) -> None:
    """Stop tracking `proc` in the current scope, if there is one.

    Args:
        proc: a process that has ended.
    """
    scope = _current.get()
    if scope is not None:
        scope.release(proc)
