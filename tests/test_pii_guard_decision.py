#!/usr/bin/env python3
"""Tests for the decision gate of `hooks/pii_guard.py` (`hooks/decision_gate.py`).

`pii_guard._scan`, the gate and `anonymize` stay real. Only the boundaries are faked: the network
(a stdlib HTTP server on a loopback port standing in for the oMLX server) and the Presidio scan
(`anonymize.detect_findings`, which loads an NLP model).
"""

from __future__ import annotations

import csv
import importlib
import json
import socket
import sys
import threading
import time
import tracemalloc
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-privacy" / "mcp"))
sys.path.insert(
    0, str(Path(__file__).parent.parent / "plugins" / "pc-privacy" / "hooks")
)
sys.path.insert(
    0, str(Path(__file__).parent.parent / "plugins" / "pc-decision" / "mcp")
)

import anonymize
import decision_client
import decision_gate
import pii_guard
from test_decision_client import (
    LOOPBACK_URLS,
    MALFORMED_STATUS_LINE,
    NON_LOOPBACK_URLS,
    TRUNCATED_CHUNKED_BODY,
    FakeServer,
    RawServer,
)

GATE_SOURCE = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-privacy"
    / "hooks"
    / "decision_gate.py"
)
PRESIDIO_FINDINGS = [anonymize.Finding("x", "contact", "detected EMAIL_ADDRESS (3/3)")]
PRESIDIO_ANON_FINDINGS = [anonymize.Finding("x", "leftover", "detected PERSON (2/3)")]
REFUSED_413 = {
    "error": {
        "message": "state does not fit the model's context window",
        "type": "DecisionContextLengthError",
    }
}
PRESIDIO_LISTING = "  - contact: detected EMAIL_ADDRESS (3/3)"
PII_MESSAGE = (
    "Personal data detected in `{name}` — reading it puts it into the model's context "
    "(and whatever cloud sits behind it):\n" + PRESIDIO_LISTING
)
DECISION_LINE = "A local decision model also judged this file to contain personal data."


def _systemone(p: float) -> dict[str, Any]:
    """Build a systemone response answering the `pii` question with probability p.

    Args:
        p: The probability of "yes".

    Returns:
        The response body.
    """
    return {
        "model": "APUS-OpenJev-v1-4B-MLX-4bit",
        "answers": {"pii": {"type": "noul", "noul": p}},
        "usage": {"input_tokens": 110, "output_tokens": 0},
    }


def _answer_with_p_of_yes(server: FakeServer, p: float) -> None:
    """Make the fake server answer the `pii` question with probability p.

    Args:
        server: The fake server.
        p: The probability of "yes".
    """
    server.answer(200, _systemone(p))


def _csv(path: Path, columns: list[str]) -> Path:
    """Write a small CSV with three rows and the given columns.

    Args:
        path: Where to write.
        columns: The column names.

    Returns:
        The path.
    """
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for i in range(3):
            w.writerow([f"{c}-value-{i}" for c in columns])
    return path


@pytest.fixture
def presidio(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Fake the Presidio scan and record how it is called.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The list of keyword arguments of every `detect_findings` call.
    """
    calls: list[dict[str, Any]] = []

    def fake(files: list[str], **kwargs: Any) -> list[anonymize.Finding]:
        calls.append(kwargs)
        return (
            PRESIDIO_ANON_FINDINGS if kwargs.get("values_only") else PRESIDIO_FINDINGS
        )

    monkeypatch.setattr(anonymize, "detect_findings", fake)
    return calls


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeServer]:
    """Run a fake oMLX server and point the gate at it.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Yields:
        The running fake server.
    """
    fake = FakeServer()
    monkeypatch.setenv("ONE_DECISION_MODEL", "APUS-OpenJev-v1-4B-MLX-4bit")
    monkeypatch.setenv("ONE_DECISION_URL", fake.url)
    yield fake
    fake.close()


class RecordingServer:
    """A loopback HTTP server recording every request, whatever its method, and answering fixed."""

    def __init__(self, status: int = 200, location: str | None = None) -> None:
        """Start serving on an ephemeral loopback port.

        Args:
            status: The HTTP status of every answer.
            location: The `Location` header of every answer, or None for no header.
        """
        self.requests: list[tuple[str, str]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            """Record the request line and answer with the owner's fixed response."""

            def handle_any(self) -> None:
                """Handle one request of any method."""
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                owner.requests.append((self.command, self.path))
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_GET = do_POST = handle_any  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:
                """Stay silent.

                Args:
                    format: Ignored.
                    *args: Ignored.
                """

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(
            target=self.httpd.serve_forever, args=(0.01,), daemon=True
        ).start()

    @property
    def url(self) -> str:
        """Base URL of the server.

        Returns:
            The loopback URL with the ephemeral port.
        """
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        """Stop serving."""
        self.httpd.shutdown()
        self.httpd.server_close()


def _closed_port() -> int:
    """Find a loopback port nothing listens on.

    Returns:
        The port.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_until(condition: Callable[[], bool]) -> None:
    """Wait up to two seconds for a condition set by a server thread.

    Args:
        condition: The condition.
    """
    deadline = time.monotonic() + 2
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)


def _scan(path: Path) -> tuple[str, str] | None:
    """Run the real hook scan.

    Args:
        path: The file.

    Returns:
        What `pii_guard._scan` returns.
    """
    return pii_guard._scan(path, anonymize)


def _presidio_only(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str] | None:
    """Run the scan with the gate switched off: the reference for every fallback.

    Args:
        path: The file.
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        What `pii_guard._scan` returns with `ONE_DECISION_MODEL` unset.
    """
    with monkeypatch.context() as m:
        m.delenv("ONE_DECISION_MODEL", raising=False)
        return _scan(path)


def test_an_anon_file_never_reaches_the_server_and_follows_the_values_only_path(
    tmp_path: Path,
    server: FakeServer,
    presidio: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server.answer(200, _systemone(0.1))
    path = _csv(tmp_path / "clients.anon.csv", ["contact", "note"])
    result = _scan(path)
    assert server.requests == []
    assert presidio == [{"values_only": True}]
    assert result == _presidio_only(path, monkeypatch)
    assert result == (
        "pii",
        "Personal data detected in `clients.anon.csv` — reading it puts it into the model's "
        "context (and whatever cloud sits behind it):\n  - leftover: detected PERSON (2/3)",
    )


@pytest.mark.parametrize("p_a", [0.9, 0.1])
def test_no_request_body_contains_the_file_name(
    tmp_path: Path, server: FakeServer, presidio: list[dict[str, Any]], p_a: float
) -> None:
    _answer_with_p_of_yes(server, p_a)
    path = _csv(tmp_path / "zzuniquename.csv", ["contact", "note"])
    _scan(path)
    assert len(server.requests) == 1
    assert "zzuniquename" not in json.dumps(server.requests)


def test_probability_just_below_the_threshold_passes_without_presidio(
    tmp_path: Path, server: FakeServer, presidio: list[dict[str, Any]]
) -> None:
    _answer_with_p_of_yes(server, 0.49)
    assert _scan(_csv(tmp_path / "d.csv", ["contact", "note"])) is None
    assert len(server.requests) == 1
    assert presidio == []


def test_probability_exactly_at_the_threshold_blocks(
    tmp_path: Path, server: FakeServer, presidio: list[dict[str, Any]]
) -> None:
    server.answer(200, _systemone(0.5))
    result = _scan(_csv(tmp_path / "d.csv", ["contact", "note"]))
    assert result == ("pii", PII_MESSAGE.format(name="d.csv") + "\n" + DECISION_LINE)


def test_block_with_presidio_findings_adds_one_line_to_the_listing(
    tmp_path: Path,
    server: FakeServer,
    presidio: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answer_with_p_of_yes(server, 0.9)
    path = _csv(tmp_path / "clients.csv", ["contact", "note"])
    result = _scan(path)
    assert presidio == [{}]
    assert result == (
        "pii",
        PII_MESSAGE.format(name="clients.csv") + "\n" + DECISION_LINE,
    )
    assert _presidio_only(path, monkeypatch) == (
        "pii",
        PII_MESSAGE.format(name="clients.csv"),
    )


def test_block_with_no_presidio_finding_names_the_file(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(anonymize, "detect_findings", lambda files, **kw: [])
    _answer_with_p_of_yes(server, 0.9)
    path = _csv(tmp_path / "names.csv", ["contact", "note"])
    assert _scan(path) == (
        "pii",
        "A local decision model judged `names.csv` to contain personal data, but no "
        "deterministic detector found a specific column — reading it puts it into the "
        "model's context (and whatever cloud sits behind it).",
    )
    assert _presidio_only(path, monkeypatch) is None


def test_the_gate_applies_to_the_prompt_submit_path(
    tmp_path: Path, server: FakeServer, presidio: list[dict[str, Any]]
) -> None:
    _answer_with_p_of_yes(server, 0.1)
    path = _csv(tmp_path / "d.csv", ["contact", "note"])
    event = {"hook_event_name": "UserPromptSubmit", "prompt": f"look at @{path}"}
    assert pii_guard.decide_prompt(event, anonymize) is None
    assert presidio == []


def test_variable_absent_falls_back_to_presidio_silently(
    tmp_path: Path,
    server: FakeServer,
    presidio: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _csv(tmp_path / "clients.csv", ["contact", "note"])
    baseline = _presidio_only(path, monkeypatch)
    capsys.readouterr()
    monkeypatch.delenv("ONE_DECISION_MODEL")
    assert _scan(path) == baseline
    assert baseline == ("pii", PII_MESSAGE.format(name="clients.csv"))
    assert presidio == [{}, {}]
    assert server.requests == []
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_closed_port_falls_back_to_presidio_silently(
    tmp_path: Path,
    presidio: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    port = _closed_port()
    path = _csv(tmp_path / "clients.csv", ["contact", "note"])
    baseline = _presidio_only(path, monkeypatch)
    capsys.readouterr()
    monkeypatch.setenv("ONE_DECISION_MODEL", "org/some-model")
    monkeypatch.setenv("ONE_DECISION_URL", f"http://127.0.0.1:{port}")
    assert _scan(path) == baseline
    assert baseline == ("pii", PII_MESSAGE.format(name="clients.csv"))
    assert presidio == [{}, {}]
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_a_state_refused_with_413_falls_back_to_presidio_after_the_whole_state_was_sent(
    tmp_path: Path,
    server: FakeServer,
    presidio: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    columns = [f"col{i:02d}" for i in range(40)]
    path = _csv(tmp_path / "wide.csv", columns)
    baseline = _presidio_only(path, monkeypatch)
    capsys.readouterr()
    server.answer(413, REFUSED_413)
    assert _scan(path) == baseline
    assert baseline == ("pii", PII_MESSAGE.format(name="wide.csv"))
    assert presidio == [{}, {}]
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
    [request] = server.requests
    assert request["path"] == "/v1/systemone"
    assert request["json"] == {
        "model": "APUS-OpenJev-v1-4B-MLX-4bit",
        "state": {
            "format": "csv",
            "columns": {c: [f"{c}-value-{i}" for i in range(3)] for c in columns},
        },
        "questions": {
            "pii": {
                "type": "noul",
                "instructions": f"{decision_gate.QUESTION} {decision_gate.RULES}",
            }
        },
        "truncate": False,
    }


def test_the_whole_request_body_is_pinned_for_a_text_file(
    tmp_path: Path, server: FakeServer
) -> None:
    path = tmp_path / "app.log"
    path.write_text("jane.doe@example.com\nsecond line\nthird line\n", encoding="utf-8")
    _answer_with_p_of_yes(server, 0.9)
    assert decision_gate.verdict(path, "text", anonymize) == "block"
    [request] = server.requests
    assert request["path"] == "/v1/systemone"
    assert request["json"] == {
        "model": "APUS-OpenJev-v1-4B-MLX-4bit",
        "state": {
            "format": "text",
            "lines": ["jane.doe@example.com", "second line", "third line"],
        },
        "questions": {
            "pii": {
                "type": "noul",
                "instructions": (
                    "Does this file contain personal data about real people that must be "
                    "anonymized before an AI model reads it? "
                    "Rules: personal data means only a person's name, an email address, a "
                    "phone number, an IBAN, a credit card number, a French SIRET, SIREN or "
                    "NIR (social security number), or a BIC. A placeholder made of a column "
                    "name, an underscore and 8 hexadecimal digits (for example "
                    "NAME_1a2b3c4d) is already anonymized and is not personal data. "
                    "Anything else is not personal data."
                ),
            }
        },
        "truncate": False,
    }


@pytest.mark.parametrize(
    "status, payload",
    [
        (503, {"error": {"type": "out_of_memory"}}),
        (400, {"error": {"message": "requires ~800MB GPU memory"}}),
    ],
)
def test_other_server_refusals_fall_back_to_presidio(
    tmp_path: Path,
    server: FakeServer,
    presidio: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    payload: dict[str, Any],
) -> None:
    path = _csv(tmp_path / "clients.csv", ["contact", "note"])
    baseline = _presidio_only(path, monkeypatch)
    server.answer(status, payload)
    assert _scan(path) == baseline
    assert baseline == ("pii", PII_MESSAGE.format(name="clients.csv"))
    assert presidio == [{}, {}]


def test_text_state_is_the_first_ten_lines_cut_at_sixty_characters(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.log"
    lines = [f"line {i:02d} " + "x" * 100 for i in range(40)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert decision_gate.build_state(path, "text", anonymize) == {
        "format": "text",
        "lines": [line[:60] for line in lines[:10]],
    }


def test_tabular_state_is_every_column_with_three_values_of_sixty_characters(
    tmp_path: Path,
) -> None:
    path = tmp_path / "t.csv"
    long_value = "y" * 100
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["a", "b"])
        for i in range(8):
            w.writerow([f"a{i}", long_value])
    assert decision_gate.build_state(path, "csv", anonymize) == {
        "format": "csv",
        "columns": {
            "a": ["a0", "a1", "a2"],
            "b": [long_value[:60]] * 3,
        },
    }


def test_a_failure_inside_the_gate_is_unavailable(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(decision_gate, "build_state", boom)
    path = _csv(tmp_path / "d.csv", ["contact", "note"])
    assert decision_gate.verdict(path, "csv", anonymize) is None
    assert server.requests == []


@pytest.mark.parametrize(
    "url", ["http://192.168.1.10:11234", "http://example.com", "ftp://127.0.0.1"]
)
def test_non_loopback_url_is_unavailable_without_a_request(
    tmp_path: Path,
    server: FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    monkeypatch.setenv("ONE_DECISION_URL", url)
    path = _csv(tmp_path / "d.csv", ["contact", "note"])
    assert decision_gate.verdict(path, "csv", anonymize) is None
    assert server.requests == []


def test_parse_probability_reads_the_noul_answer() -> None:
    assert decision_gate.parse_probability(_systemone(0.9768)) == 0.9768
    assert decision_gate.parse_probability(_systemone(0.0)) == 0.0
    assert decision_gate.parse_probability(_systemone(1.0)) == 1.0


@pytest.mark.parametrize("bad", [1.5, -0.2])
def test_parse_probability_rejects_out_of_range(bad: float) -> None:
    assert decision_gate.parse_probability(_systemone(bad)) is None
    minimal = {"answers": {"pii": {"type": "noul", "noul": 0}}}
    assert decision_gate.parse_probability(minimal) == 0.0


@pytest.mark.parametrize(
    "payload",
    [
        # A chat-completions response: a foreign shape for the systemone endpoint.
        {
            "choices": [
                {
                    "logprobs": {
                        "content": [{"top_logprobs": [{"token": "A", "logprob": -0.1}]}]
                    }
                }
            ]
        },
        {"answers": {}},
        {"answers": {"other": {"type": "noul", "noul": 0.9}}},
        {"answers": {"pii": {}}},
        {"answers": {"pii": {"noul": "high"}}},
        {"answers": {"pii": {"noul": float("inf")}}},
        {"answers": {"pii": {"noul": float("nan")}}},
        {"answers": []},
        {},
        None,
        [1],
    ],
)
def test_parse_probability_rejects_any_other_shape(payload: Any) -> None:
    assert decision_gate.parse_probability(payload) is None


def test_the_gate_starts_no_subprocess() -> None:
    source = GATE_SOURCE.read_text(encoding="utf-8")
    assert [w for w in ("subprocess", "os.system", "Popen") if w in source] == []


def test_a_down_server_is_probed_before_the_state_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[Path] = []

    def recording_state(path: Path, fmt: str, module: Any) -> dict[str, Any]:
        built.append(path)
        raise AssertionError("the state was built for a server that is down")

    monkeypatch.setenv("ONE_DECISION_MODEL", "APUS-OpenJev-v1-4B-MLX-4bit")
    monkeypatch.setenv("ONE_DECISION_URL", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setattr(decision_gate, "build_state", recording_state)
    path = _csv(tmp_path / "d.csv", ["contact", "note"])
    assert decision_gate.probability(path, "csv", anonymize) is None
    assert built == []


def test_an_unreachable_probe_switches_the_gate_off_for_the_process(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _csv(tmp_path / "d.csv", ["contact", "note"])
    _answer_with_p_of_yes(server, 0.9)
    with monkeypatch.context() as m:
        m.setenv("ONE_DECISION_URL", f"http://127.0.0.1:{_closed_port()}")
        assert decision_gate.verdict(path, "csv", anonymize) is None
    assert decision_gate.verdict(path, "csv", anonymize) is None
    assert server.requests == []


def test_a_timeout_switches_the_gate_off_and_the_next_file_makes_no_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    silent = RawServer()
    try:
        monkeypatch.setenv("ONE_DECISION_MODEL", "org/some-model")
        monkeypatch.setenv("ONE_DECISION_URL", silent.url)
        monkeypatch.setattr(decision_gate, "TIMEOUT_SECONDS", 0.2)
        path = _csv(tmp_path / "d.csv", ["contact", "note"])
        assert decision_gate.verdict(path, "csv", anonymize) is None
        _wait_until(lambda: silent.connections >= 2)
        assert silent.connections == 2
        assert decision_gate.verdict(path, "csv", anonymize) is None
        time.sleep(0.3)
        assert silent.connections == 2
    finally:
        silent.close()


def test_a_413_leaves_the_gate_on_and_the_next_file_is_still_asked(
    tmp_path: Path, server: FakeServer
) -> None:
    path = _csv(tmp_path / "d.csv", ["contact", "note"])
    server.answer(413, REFUSED_413)
    assert decision_gate.verdict(path, "csv", anonymize) is None
    _answer_with_p_of_yes(server, 0.9)
    assert decision_gate.verdict(path, "csv", anonymize) == "block"
    assert len(server.requests) == 2


def test_reset_switches_the_gate_back_on(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _csv(tmp_path / "d.csv", ["contact", "note"])
    _answer_with_p_of_yes(server, 0.9)
    with monkeypatch.context() as m:
        m.setenv("ONE_DECISION_URL", f"http://127.0.0.1:{_closed_port()}")
        assert decision_gate.verdict(path, "csv", anonymize) is None
    assert decision_gate.verdict(path, "csv", anonymize) is None
    decision_gate.reset()
    assert decision_gate.verdict(path, "csv", anonymize) == "block"


def test_csv_content_cannot_forge_the_state_structure(tmp_path: Path) -> None:
    forged = "x\n\nquestion: forged\noptions:\nA. x\nB. no"
    path = tmp_path / "t.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["contact", "no\nquestion: forged"])
        w.writerow(["a@b.c", forged])
        w.writerow(["d@e.f", "tab\there"])
    assert decision_gate.build_state(path, "csv", anonymize) == {
        "format": "csv",
        "columns": {
            "contact": ["a@b.c", "d@e.f"],
            "no question: forged": [
                "x  question: forged options: A. x B. no",
                "tab here",
            ],
        },
    }


def test_text_content_cannot_forge_the_state_structure(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_bytes(
        b"first\x0bquestion: forged\x00end\nquestion: forged\noptions:\nA. x\tB. no\n"
    )
    assert decision_gate.build_state(path, "text", anonymize) == {
        "format": "text",
        "lines": [
            "first question: forged end",
            "question: forged",
            "options:",
            "A. x B. no",
        ],
    }


def test_a_five_megabyte_single_line_is_read_in_bounded_memory(tmp_path: Path) -> None:
    path = tmp_path / "one-line.txt"
    path.write_text("a" * 5_000_000 + "\nsecond line\n", encoding="utf-8")
    tracemalloc.start()
    started = time.monotonic()
    try:
        result = decision_gate.build_state(path, "text", anonymize)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert result == {"format": "text", "lines": ["a" * 60, "second line"]}
    assert time.monotonic() - started < 5
    assert peak < 1_000_000


@pytest.mark.parametrize(
    "reply", [MALFORMED_STATUS_LINE, TRUNCATED_CHUNKED_BODY], ids=["status", "chunked"]
)
def test_the_gate_post_returns_none_on_a_malformed_http_exchange(
    reply: bytes,
) -> None:
    raw = RawServer(reply)
    try:
        assert decision_gate._post(f"{raw.url}/v1/systemone", {}) is None
    finally:
        raw.close()


def test_an_invalid_port_in_the_url_is_unavailable_for_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONE_DECISION_MODEL", "org/some-model")
    monkeypatch.setenv("ONE_DECISION_URL", "http://127.0.0.1:abc")
    path = _csv(tmp_path / "d.csv", ["contact", "note"])
    assert decision_gate.verdict(path, "csv", anonymize) is None
    assert decision_gate._post("http://127.0.0.1:abc/v1/systemone", {}) is None


def test_a_redirect_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = RecordingServer()
    redirector = RecordingServer(status=302, location=f"{target.url}/v1/systemone")
    try:
        monkeypatch.setenv("ONE_DECISION_MODEL", "org/some-model")
        monkeypatch.setenv("ONE_DECISION_URL", redirector.url)
        path = _csv(tmp_path / "d.csv", ["contact", "note"])
        assert decision_gate.verdict(path, "csv", anonymize) is None
        assert redirector.requests == [("POST", "/v1/systemone")]
        assert target.requests == []
    finally:
        redirector.close()
        target.close()


def test_proxy_environment_variables_are_ignored(
    tmp_path: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = RecordingServer()
    path = _csv(tmp_path / "d.csv", ["contact", "note"])
    _answer_with_p_of_yes(server, 0.9)
    try:
        with monkeypatch.context() as m:
            for name in (
                "HTTP_PROXY",
                "http_proxy",
                "HTTPS_PROXY",
                "https_proxy",
                "ALL_PROXY",
                "all_proxy",
            ):
                m.setenv(name, proxy.url)
            m.delenv("NO_PROXY", raising=False)
            m.delenv("no_proxy", raising=False)
            importlib.reload(decision_gate)
            assert decision_gate.verdict(path, "csv", anonymize) == "block"
        assert len(server.requests) == 1
        assert proxy.requests == []
    finally:
        importlib.reload(decision_gate)
        proxy.close()


def test_a_server_that_never_replies_is_unavailable_within_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    silent = RawServer()
    try:
        monkeypatch.setenv("ONE_DECISION_MODEL", "org/some-model")
        monkeypatch.setenv("ONE_DECISION_URL", silent.url)
        monkeypatch.setattr(decision_gate, "TIMEOUT_SECONDS", 0.3)
        path = _csv(tmp_path / "d.csv", ["contact", "note"])
        started = time.monotonic()
        assert decision_gate.verdict(path, "csv", anonymize) is None
        assert time.monotonic() - started < 5
    finally:
        silent.close()


@pytest.mark.parametrize("url", NON_LOOPBACK_URLS + LOOPBACK_URLS)
def test_the_gate_and_the_client_agree_on_which_urls_are_loopback(url: str) -> None:
    assert decision_gate._is_loopback(url) == decision_client._is_loopback(url)
    assert decision_gate._is_loopback(url) == (url in LOOPBACK_URLS)


def test_a_model_pass_stops_the_guard_before_presidio_even_past_the_text_line_cap(
    tmp_path: Path, server: FakeServer, presidio: list[dict[str, Any]]
) -> None:
    path = tmp_path / "long.log"
    lines = [
        f"2024-01-01 INFO request {i} handled"
        for i in range(pii_guard.MAX_TEXT_LINES + 500)
    ]
    lines[-1] = "2024-01-01 INFO contact jane.doe@example.com"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _answer_with_p_of_yes(server, 0.1)
    assert _scan(path) is None
    assert presidio == []
    assert len(server.requests) == 1
    assert "jane.doe@example.com" not in json.dumps(server.requests)
