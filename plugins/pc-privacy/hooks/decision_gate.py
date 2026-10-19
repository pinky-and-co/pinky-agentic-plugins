"""File-level decision gate of `pii_guard`: ask a local decision model whether a file is personal data.

Standard library only, in the hook's own process: a `command` hook cannot call an MCP tool and
must keep the Presidio fallback in-process. The model comes from `ONE_DECISION_MODEL` (an oMLX
decision-model id), the server address from `LOCAL_INFERENCE_BASE_URL` (`http://host:port` without `/v1`, loopback hosts only, no built-in default; unset
or empty: the gate is off). The server is started by the person, never by this module.

The gate sends the file's state (never the file name) as a JSON object to oMLX
`POST /v1/systemone`: the first sampled values of each column, or the first lines of a text
file, long values cut. The question is a yes/no one (`noul`) whose instructions carry the
question and the guard's rules; the server builds the prompt the model was trained on and
returns the probability of "yes". `truncate` is always false: a state is never silently cut
server-side, and a state that does not fit the model's window is refused with an HTTP error.
Every failure makes `verdict` return None, and the caller then runs its own detector: a server
down, a refused state, any HTTP error. A server that cannot be reached is probed before the
state is built, and after a transport failure the gate stays off for the rest of the process,
so a down server costs neither a second parse of a large file nor a timeout per file. Nothing
is logged or printed, and the state never leaves a loopback connection.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

MODEL_ENV = "ONE_DECISION_MODEL"
URL_ENV = "LOCAL_INFERENCE_BASE_URL"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TIMEOUT_SECONDS = 10
PROBE_TIMEOUT_SECONDS = 0.5
MAX_RESPONSE_BYTES = 1_000_000
THRESHOLD = 0.5

VALUES_PER_COLUMN = 3
VALUE_MAX_CHARS = 60
TEXT_LINES = 10
LINE_READ_LIMIT = 4 * VALUE_MAX_CHARS

_CONTROL_CHARS = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_tripped = False

QUESTION = (
    "Does this file contain personal data about real people that must be "
    "anonymized before an AI model reads it?"
)
RULES = (
    "Rules: personal data means only a person's name, an email address, a phone number, "
    "an IBAN, a credit card number, a French SIRET, SIREN or NIR (social security number), "
    "or a BIC. A placeholder made of a column name, an underscore and 8 hexadecimal digits "
    "(for example NAME_1a2b3c4d) is already anonymized and is not personal data. "
    "Anything else is not personal data."
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a loopback server cannot send the request elsewhere."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        """Decline every redirect.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Returns:
            None, which makes urllib raise the redirect response as an HTTP error.
        """
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def reset() -> None:
    """Switch the gate back on after a transport failure switched it off."""
    global _tripped
    _tripped = False


def _trip() -> None:
    """Switch the gate off for the rest of the process."""
    global _tripped
    _tripped = True


def resolve_base_url() -> str | None:
    """Read the local inference server address from `LOCAL_INFERENCE_BASE_URL`, no default.

    Same resolution as `pc-core`'s subagent-routing `classify.resolve_base_url`: plugins are
    installed apart, so one cannot import the other's module.

    Returns:
        The variable's value, or None when it is unset or empty.
    """
    return os.environ.get(URL_ENV) or None


def _is_loopback(url: str) -> bool:
    """Tell whether a base URL is an http(s) URL on a loopback host.

    Args:
        url: The base URL.

    Returns:
        True when the scheme is http or https and the host is a loopback host.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        return parts.scheme in ("http", "https") and parts.hostname in LOOPBACK_HOSTS
    except ValueError:
        return False


def _flatten(text: str) -> str:
    """Replace every newline, carriage return, tab and other control character by a space.

    Args:
        text: A value, a column name or a line taken from the file.

    Returns:
        The text on one line, so file content cannot forge a field of the state.
    """
    return _CONTROL_CHARS.sub(" ", text)


def _first_lines(path: Path) -> list[str]:
    """Read the first lines of a text file without holding an over-long line in memory.

    Args:
        path: The file.

    Returns:
        At most TEXT_LINES lines, each cut at VALUE_MAX_CHARS, without their line ending.
    """
    lines: list[str] = []
    with path.open(encoding="utf-8", errors="ignore") as handle:
        while len(lines) < TEXT_LINES:
            raw = handle.readline(LINE_READ_LIMIT)
            if not raw:
                break
            lines.append(raw.rstrip("\r\n")[:VALUE_MAX_CHARS])
            while len(raw) == LINE_READ_LIMIT and not raw.endswith("\n"):
                raw = handle.readline(LINE_READ_LIMIT)
    return lines


def build_state(path: Path, fmt: str, anonymize: Any) -> dict[str, Any]:
    """Build the state sent to the decision model.

    A tabular file gives its format and, for every column, its first sampled values, each cut
    at VALUE_MAX_CHARS. A text file gives its format and its first TEXT_LINES lines, each cut
    the same way. Control characters become spaces, so the content stays in its own field. A
    state too large for the model's window is refused by the server (`truncate` stays false),
    which is the signal to fall back.

    Args:
        path: The file.
        fmt: Its format, as decided by `anonymize.detect_format`.
        anonymize: The `anonymize` module, for its column sampling.

    Returns:
        The JSON-serialisable state.
    """
    if fmt == "text":
        return {
            "format": fmt,
            "lines": [_flatten(line) for line in _first_lines(path)],
        }
    return {
        "format": fmt,
        "columns": {
            _flatten(str(name)): [
                _flatten(str(v)[:VALUE_MAX_CHARS]) for v in values[:VALUES_PER_COLUMN]
            ]
            for name, values in anonymize._column_samples(path, fmt).items()
        },
    }


def build_request_body(model: str, state: dict[str, Any]) -> dict[str, Any]:
    """Build the systemone body asking the `pii` yes/no question about the state.

    Args:
        model: The oMLX decision-model id.
        state: The file state.

    Returns:
        The JSON-serialisable request body.
    """
    return {
        "model": model,
        "state": state,
        "questions": {"pii": {"type": "noul", "instructions": f"{QUESTION} {RULES}"}},
        "truncate": False,
    }


def parse_probability(payload: Any) -> float | None:
    """Read the probability of "yes" from the answer to the `pii` question.

    Args:
        payload: The decoded systemone response.

    Returns:
        The probability of personal data (`answers.pii.noul`), or None when the response has
        another shape.
    """
    try:
        probability = float(payload["answers"]["pii"]["noul"])
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        return None
    return probability


def _reachable(base: str) -> bool:
    """Open and close a TCP connection to the server's host and port.

    Args:
        base: The base URL, already checked as loopback.

    Returns:
        True when the connection is accepted within PROBE_TIMEOUT_SECONDS.
    """
    try:
        parts = urllib.parse.urlsplit(base)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        with socket.create_connection(
            (parts.hostname or "", port), PROBE_TIMEOUT_SECONDS
        ):
            return True
    except (OSError, ValueError):
        return False


def _post(url: str, body: dict[str, Any]) -> Any:
    """Send the request and decode the JSON response.

    A transport failure switches the gate off for the rest of the process. An HTTP error status
    or an undecodable body concerns this file only and leaves the gate on.

    Args:
        url: The systemone endpoint, already checked as loopback.
        body: The request body.

    Returns:
        The decoded response, or None on any transport, HTTP or decoding failure.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            return json.loads(response.read(MAX_RESPONSE_BYTES))
    except urllib.error.HTTPError:
        return None
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        _trip()
        return None
    except ValueError:
        return None


def probability(path: Path, fmt: str, anonymize: Any) -> float | None:
    """Ask the local decision model for the probability that a file holds personal data.

    Args:
        path: The file.
        fmt: Its format, as decided by `anonymize.detect_format`.
        anonymize: The `anonymize` module, for its column sampling.

    Returns:
        The probability of personal data, or None when the gate is unavailable for any reason.
    """
    try:
        model = os.environ.get(MODEL_ENV, "")
        base = resolve_base_url()
        if _tripped or not model or base is None or not _is_loopback(base):
            return None
        if not _reachable(base):
            _trip()
            return None
        body = build_request_body(model, build_state(path, fmt, anonymize))
        return parse_probability(_post(f"{base.rstrip('/')}/v1/systemone", body))
    except Exception:
        return None


def verdict(path: Path, fmt: str, anonymize: Any) -> str | None:
    """Judge a file with the local decision model.

    Args:
        path: The file.
        fmt: Its format, as decided by `anonymize.detect_format`.
        anonymize: The `anonymize` module, for its column sampling.

    Returns:
        "block" when the probability of personal data reaches THRESHOLD, "pass" when it stays
        below, None when the gate is unavailable for any reason.
    """
    p = probability(path, fmt, anonymize)
    if p is None:
        return None
    return "block" if p >= THRESHOLD else "pass"
