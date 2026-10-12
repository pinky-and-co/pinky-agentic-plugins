"""A minimal stdio MCP client for the pc-snow server tests.

Speaks newline-delimited JSON-RPC to a server subprocess, answers the server's own requests
(`elicitation/create`) from a callback, and keeps every raw stdout line so a test can assert that
nothing but protocol messages was written there.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-11-25"

ElicitAnswer = Callable[[dict[str, Any]], dict[str, Any]]


class StdioMcp:
    """One server subprocess and the conversation held with it."""

    def __init__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: Path | None = None,
        elicit: ElicitAnswer | None = None,
    ) -> None:
        """Start the server; `elicit` set means the client declares the elicitation capability."""
        self._elicit = elicit
        self._next_id = 0
        self.stdout_lines: list[str] = []
        self.elicitation_requests: list[dict[str, Any]] = []
        self._process = subprocess.Popen(  # noqa: S603
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=dict(env),
            cwd=cwd,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        """Move the server's stdout into a queue so reads can time out."""
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        """Write one JSON-RPC message."""
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()

    def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 120
    ) -> dict[str, Any]:
        """Send a request and return its `result`, answering server requests meanwhile."""
        self._next_id += 1
        request_id = self._next_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        while True:
            raw = self._lines.get(timeout=timeout)
            if raw is None:
                raise RuntimeError("server closed stdout")
            self.stdout_lines.append(raw)
            message = json.loads(raw)
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message["result"]
            if message.get("method") == "elicitation/create":
                self._answer_elicitation(message)

    def _answer_elicitation(self, message: dict[str, Any]) -> None:
        """Record the server's elicitation request and reply with the callback's answer."""
        self.elicitation_requests.append(message["params"])
        assert self._elicit is not None
        self._send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": self._elicit(message["params"]),
            }
        )

    def initialize(self) -> dict[str, Any]:
        """Run the initialize handshake."""
        capabilities: dict[str, Any] = {"elicitation": {}} if self._elicit else {}
        result = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": capabilities,
                "clientInfo": {"name": "pc-snow-tests", "version": "0"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        """The server's announced tools."""
        return list(self.request("tools/list")["tools"])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool and return the raw `tools/call` result."""
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        """Stop the server."""
        if self._process.stdin is not None:
            self._process.stdin.close()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
