"""Shared fake local model servers for hook tests.

A `FakeServer` records every request body and replays one canned JSON answer over real loopback
HTTP; a `RawServer` counts connections and answers with fixed bytes, or never answers at all. The
URL lists pin which base URLs a client must accept as loopback and which it must refuse.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

NON_LOOPBACK_URLS = [
    "http://192.168.1.10:11234",
    "http://example.com",
    "http://127.0.0.1.example.com:11234",
    "http://localhost@example.com",
    "http://0.0.0.0:11234",
    "ftp://127.0.0.1",
    "not a url",
]
LOOPBACK_URLS = [
    "http://127.0.0.1:11234",
    "http://localhost:11234",
    "http://[::1]:11234",
    "https://127.0.0.1",
]
MALFORMED_STATUS_LINE = b"garbage\r\n\r\n"
TRUNCATED_CHUNKED_BODY = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
    b'Transfer-Encoding: chunked\r\n\r\n10\r\n{"a":'
)


class FakeServer:
    """A fake local model server recording every request body and replaying one canned answer."""

    def __init__(self) -> None:
        """Start serving on an ephemeral loopback port."""
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.body: bytes = b"{}"
        self.delay = 0.0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            """Record the POST body and answer with the owner's canned response."""

            def do_POST(self) -> None:  # noqa: N802
                """Handle one chat-completions request."""
                length = int(self.headers["Content-Length"])
                owner.requests.append(
                    {"path": self.path, "json": json.loads(self.rfile.read(length))}
                )
                time.sleep(owner.delay)
                try:
                    self.send_response(owner.status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(owner.body)
                except OSError:
                    pass

            def log_message(self, format: str, *args: Any) -> None:
                """Stay silent.

                Args:
                    format: Ignored.
                    *args: Ignored.
                """

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, args=(0.01,), daemon=True
        )
        self.thread.start()

    @property
    def url(self) -> str:
        """Base URL of the fake server.

        Returns:
            The loopback URL with the ephemeral port.
        """
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def answer(self, status: int, payload: Any) -> None:
        """Set the canned answer.

        Args:
            status: The HTTP status.
            payload: A JSON-serialisable body, or raw bytes sent as is.
        """
        self.status = status
        self.body = (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        )

    def close(self) -> None:
        """Stop serving."""
        self.httpd.shutdown()
        self.httpd.server_close()


class RawServer:
    """A raw-socket fake server: it counts connections and answers with fixed bytes, or never."""

    def __init__(self, reply: bytes | None = None) -> None:
        """Start listening on an ephemeral loopback port.

        Args:
            reply: Bytes sent verbatim once a request has been read, then the connection is
                closed. None keeps every connection open and silent.
        """
        self.reply = reply
        self.connections = 0
        self._held: list[socket.socket] = []
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """Base URL of the server.

        Returns:
            The loopback URL with the ephemeral port.
        """
        return f"http://127.0.0.1:{self._listener.getsockname()[1]}"

    def _serve(self) -> None:
        """Accept connections until the listener is closed."""
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            self.connections += 1
            if self.reply is None:
                self._held.append(conn)
                continue
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn: socket.socket) -> None:
        """Read one whole request, send the canned bytes and close.

        Args:
            conn: The accepted connection.
        """
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.lower() == b"content-length":
                    length = int(value)
            while len(rest) < length:
                rest += conn.recv(65536)
            assert self.reply is not None
            conn.sendall(self.reply)

    def close(self) -> None:
        """Stop listening and drop held connections."""
        self._listener.close()
        for conn in self._held:
            conn.close()
