"""The real `pc_models_server.py` as a subprocess, with a fake `pi` that never answers.

The server runs with its own interpreter, a pool of `pool_size` entries from distinct providers
declared in a throwaway project, and a `pi` shell script first in its `PATH`. That script records
each launch (its pid and its child's) in `launches_dir` and then waits on a long `sleep`, so a test
can tell, from the process table alone, whether the server killed the process and its descendant.
Only the model boundary is fake: the server, its tools and the MCP protocol are the real ones.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-11-25"
SERVER = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-models"
    / "mcp"
    / "pc_models_server.py"
)

_FAKE_PI = """#!/bin/sh
model=
while [ $# -gt 0 ]; do
  if [ "$1" = "--model" ]; then model="$2"; fi
  shift
done
echo "$$ $model" > "$FAKE_PI_DIR/launched.$$"
sleep 300 &
echo "$!" > "$FAKE_PI_DIR/child.$$"
wait
"""


def alive(pid: int) -> bool:
    """Whether `pid` is a running process (a zombie awaiting its reaper does not count).

    Args:
        pid: the process id to look for.

    Returns:
        True when the process exists and has not ended.
    """
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


class PoolServer:
    """One server subprocess, driven over newline-delimited JSON-RPC on its stdio."""

    def __init__(
        self, root: Path, pool_size: int = 2, extra_env: dict[str, str] | None = None
    ) -> None:
        """Start the server and run the `initialize` handshake.

        Args:
            root: an empty directory the server's project, `HOME` and fake `pi` live in.
            pool_size: how many entries the project's `model_pool` declares.
            extra_env: variables layered over the server's environment (a short wall cap, …).
        """
        self.launches_dir = root / "launches"
        self.launches_dir.mkdir()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        fake_pi = bin_dir / "pi"
        fake_pi.write_text(_FAKE_PI)
        fake_pi.chmod(0o755)
        project = root / "project"
        (project / ".claude").mkdir(parents=True)
        pool = [
            {
                "name": f"fake-{i}",
                "kind": "cli",
                "provider": f"provider-{i}",
                "model": f"model-{i}",
                "capacity_tier": "sonnet",
                "free_tier": True,
                "concurrency": None,
            }
            for i in range(pool_size)
        ]
        (project / ".claude" / "settings.pinky.json").write_text(
            json.dumps({"model_pool": pool})
        )
        (root / "home").mkdir()
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_PI_DIR": str(self.launches_dir),
            "CLAUDE_PROJECT_DIR": str(project),
            "HOME": str(root / "home"),
            **(extra_env or {}),
        }
        env.pop("CMUX_SURFACE_ID", None)
        self._stderr = open(root / "server.err", "w")
        self.process = subprocess.Popen(  # noqa: S603
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            env=env,
            cwd=project,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._next_id = 0
        self.answered: set[int] = set()
        self._send_request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pc-models-tests", "version": "0"},
            },
        )
        self._await(1)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _pump(self) -> None:
        """Move the server's stdout into a queue so reads can time out."""
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        """Write one JSON-RPC message."""
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _send_request(self, method: str, params: dict[str, Any]) -> int:
        """Write a request without waiting for its response."""
        self._next_id += 1
        self._send(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        )
        return self._next_id

    def _await(self, request_id: int) -> None:
        """Read the server's output until the response to `request_id` arrives."""
        while request_id not in self.answered:
            raw = self._lines.get(timeout=30)
            if raw is None:
                raise RuntimeError("server closed stdout")
            message = json.loads(raw)
            if "method" not in message and "id" in message:
                self.answered.add(message["id"])

    def drain(self) -> None:
        """Record every response the server has written so far."""
        while True:
            try:
                raw = self._lines.get_nowait()
            except queue.Empty:
                return
            if raw is None:
                return
            message = json.loads(raw)
            if "method" not in message and "id" in message:
                self.answered.add(message["id"])

    def start_call(self, tool: str, arguments: dict[str, Any]) -> int:
        """Send a `tools/call` and return its request id without waiting for the answer.

        Args:
            tool: the tool's name.
            arguments: its arguments.

        Returns:
            The JSON-RPC id of the request.
        """
        return self._send_request("tools/call", {"name": tool, "arguments": arguments})

    def list_tools(self) -> list[dict[str, Any]]:
        """Ask the server for `tools/list` and wait for the answer.

        Returns:
            The tools the server advertises, each with its `name` and `inputSchema`.
        """
        request_id = self._send_request("tools/list", {})
        while True:
            raw = self._lines.get(timeout=30)
            if raw is None:
                raise RuntimeError("server closed stdout")
            message = json.loads(raw)
            if "method" not in message and message.get("id") == request_id:
                return message["result"]["tools"]

    def cancel(self, request_id: int) -> None:
        """Send `notifications/cancelled` for `request_id`, as a client giving up does."""
        self._send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": request_id, "reason": "test"},
            }
        )

    def close_stdin(self) -> None:
        """Close the server's input, as a client that went away does."""
        assert self.process.stdin is not None
        self.process.stdin.close()

    def launched(self) -> list[tuple[int, int]]:
        """The fake `pi` that started and have a child, as `(pi pid, its child's pid)`."""
        return [(pi, child) for pi, child, _ in self._launches() if child]

    def launch_count(self) -> int:
        """How many fake `pi` started, including one killed before it spawned its child."""
        return len(self._launches())

    def _launches(self) -> list[tuple[int, int, str]]:
        """Every fake `pi` that started, as `(pi pid, its child's pid or 0, its `--model`)`."""
        out = []
        for record in self.launches_dir.glob("launched.*"):
            fields = record.read_text().split()
            if len(fields) != 2:
                continue
            child = self.launches_dir / f"child.{fields[0]}"
            child_pid = int(child.read_text() or 0) if child.exists() else 0
            out.append((int(fields[0]), child_pid, fields[1]))
        return out

    def all_pids(self) -> list[int]:
        """The pids of every fake `pi` that started and of its child."""
        return [pid for pi, child, _ in self._launches() for pid in (pi, child) if pid]

    def pids_of(self, model: str) -> list[int]:
        """The pids of the fake `pi` and of its child launched for `model` (`--model` ends with it)."""
        return [
            pid
            for pi, child, m in self._launches()
            if m.endswith(model)
            for pid in (pi, child)
            if pid
        ]

    def wait_launched(self, count: int, timeout: float = 30.0) -> list[tuple[int, int]]:
        """Wait until `count` fake `pi` are running with their child; return their pids."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.launched()
            if len(found) >= count and all(alive(c) for _, c in found):
                return found
            time.sleep(0.05)
        raise TimeoutError(f"{len(self.launched())} fake pi launched, wanted {count}")

    def wait_gone(self, pids: list[int], timeout: float) -> bool:
        """Whether every pid in `pids` is gone within `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(alive(p) for p in pids):
                return True
            time.sleep(0.05)
        return not any(alive(p) for p in pids)

    def wait_exit(self, timeout: float) -> bool:
        """Whether the server process ended within `timeout` seconds."""
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    def stderr_text(self) -> str:
        """What the server has written to its stderr so far (its kill log)."""
        self._stderr.flush()
        return Path(self._stderr.name).read_text(encoding="utf-8", errors="replace")

    def wait_response(self, request_id: int, timeout: float = 30.0) -> dict:
        """Read the server's output until the response to `request_id` arrives; return it.

        Args:
            request_id: the JSON-RPC id of the request.
            timeout: seconds to wait.

        Returns:
            The response message — a `result` or an `error`, whichever the server sent.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self._lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if raw is None:
                raise RuntimeError("server closed stdout")
            message = json.loads(raw)
            if "method" not in message and message.get("id") == request_id:
                self.answered.add(request_id)
                return message
        raise TimeoutError(f"no response to request {request_id}")

    def wait_answered(self, request_id: int, timeout: float = 30.0) -> bool:
        """Whether the response to `request_id` arrives within `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain()
            if request_id in self.answered:
                return True
            time.sleep(0.05)
        return False

    def stop(self) -> None:
        """End the server and every fake `pi` it left, whatever the test saw."""
        for pi, child, _ in self._launches():
            for pid in (pi, child):
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
        self.process.kill()
        self.process.wait()
        self._stderr.close()
