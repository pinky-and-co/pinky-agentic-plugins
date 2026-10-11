#!/usr/bin/env python3
"""Follow a pool run live — one readable line per `pi --mode json` event.

Opened in a cmux split by `model_adversarial.run_pool` while a pool entry runs, so a call that
lasts minutes shows its text as it is written, its tool steps, its provider errors (429, 502), its
retries and its end. A read-only spectator of one events file: the pool process copies every event
line of every `pi` call into it (with a `pool_attempt` marker before each attempt and a `pool_end`
at the end), so this script never runs `pi` and needs no provider key.

Stdlib only — it runs under whichever interpreter the split was given.

Usage:
    pi_events_view.py <events-file> [--idle-timeout SECONDS]
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

IDLE_TIMEOUT = 1800.0
_POLL_SECONDS = 0.2
_ARG_CHARS = 60
# Unicode categories that hide or reorder text (controls, format characters, line and paragraph
# separators); `_is_hidden` adds the blocks and marks that carry no category of their own.
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
# ASCII whitespace only: `str.split()` would also swallow U+2028/U+2029 and U+0085, which must
# reach `_plain` to be replaced.
_ASCII_SPACE_RE = re.compile(r"[ \t\n\r\f\v]+")


def _is_hidden(char: str) -> bool:
    """Whether `char` could hide or reorder text on a terminal: a control (ESC included), a format
    or separator character, the whole U+2060-U+206F block (unassigned points included), the
    combining grapheme joiner and the variation selectors (Mongolian free ones, the standard block
    and the supplement). Newline and tab are not.
    """
    if char in "\n\t":
        return False
    return (
        unicodedata.category(char) in _HIDDEN_CATEGORIES
        or "\u2060" <= char <= "\u206f"
        or char == "\u034f"
        or "\u180b" <= char <= "\u180d"
        or "\ufe00" <= char <= "\ufe0f"
        or "\U000e0100" <= char <= "\U000e01ef"
    )


def _plain(text: str) -> str:
    """`text` with every hidden character replaced by `?`."""
    return "".join("?" if _is_hidden(char) else char for char in text)


def _squash(text: str) -> str:
    """`text` on one line: every run of ASCII whitespace, newlines included, becomes one space."""
    return " ".join(part for part in _ASCII_SPACE_RE.split(text) if part)


def _summarize(value: Any) -> str:
    """One tool argument value as short text, cut at `_ARG_CHARS` with a trailing `...`."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = _squash(text)
    if len(text) > _ARG_CHARS:
        return text[: _ARG_CHARS - 3] + "..."
    return text


def _call(name: Any, args: Any) -> str:
    """A tool call as `name(key=value, ...)`, the values summarized."""
    shown = ", ".join(
        f"{k}={_summarize(v)}"
        for k, v in (args.items() if isinstance(args, dict) else [])
    )
    return f"{name}({shown})"


class EventRenderer:
    """Turns the events of a run into the text the split prints.

    Text deltas are streamed inline, so the renderer keeps whether a text block is open; every
    other line closes it first.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        """Start with no text block open.

        Args:
            clock: seconds elapsed since the viewer started; read once per event.
        """
        self._clock = clock
        self._in_text = False
        self.finished = False

    def feed(self, record: dict[str, Any]) -> str:
        """The text to print for one event, hidden characters replaced by `?`.

        Args:
            record: one decoded line of the events file.

        Returns:
            The text — possibly empty, possibly a fragment without its newline (a text delta).
        """
        return _plain(self._render(record))

    def _render(self, record: dict[str, Any]) -> str:
        """The text for one event, before control characters are replaced."""
        stamp = f"[{self._clock():5.1f}s] "
        kind = record.get("type")
        if kind == "message_update":
            update = record.get("assistantMessageEvent")
            return self._update(stamp, update if isinstance(update, dict) else {})
        line = self._line(stamp, kind, record)
        if line is None:
            return ""
        closing = "\n" if self._in_text else ""
        self._in_text = False
        return closing + line + "\n"

    def _update(self, stamp: str, update: dict[str, Any]) -> str:
        """The text for one `message_update`: a text delta streams inline, thinking is a line."""
        kind = update.get("type")
        if kind == "text_delta":
            delta = str(update.get("delta", ""))
            if self._in_text:
                return delta
            self._in_text = True
            return f"{stamp}text: {delta}"
        if kind == "text_end" and self._in_text:
            self._in_text = False
            return "\n"
        if kind == "thinking_start":
            closing = "\n" if self._in_text else ""
            self._in_text = False
            return f"{closing}{stamp}thinking\n"
        return ""

    def _line(self, stamp: str, kind: Any, record: dict[str, Any]) -> str | None:
        """The one line an event stands for, without its newline; None for an event not shown."""
        if kind == "pool_attempt":
            return (
                f"{stamp}== {record.get('entry')} (attempt {record.get('attempt')}) =="
            )
        if kind == "pool_result":
            return (
                f"{stamp}-> {record.get('entry')} attempt {record.get('attempt')}: "
                f"{record.get('outcome')}"
            )
        if kind == "tool_execution_start":
            return f"{stamp}tool {_call(record.get('toolName'), record.get('args'))}"
        if kind == "tool_execution_end":
            outcome = "error" if record.get("isError") else "ok"
            took = record.get("durationMs", "?")
            return f"{stamp}  <- {record.get('toolName')} {outcome} {took} ms"
        if kind == "message_end":
            message = record.get("message")
            if (
                isinstance(message, dict)
                and message.get("role") == "assistant"
                and message.get("stopReason") == "error"
            ):
                return f"{stamp}error {_squash(str(message.get('errorMessage', '')))}"
            return None
        if kind == "auto_retry_start":
            return (
                f"{stamp}retry {record.get('attempt')}/{record.get('maxAttempts')} "
                f"in {record.get('delayMs')} ms: {_squash(str(record.get('errorMessage', '')))}"
            )
        if kind == "auto_retry_end":
            if record.get("success"):
                return f"{stamp}retry succeeded (attempt {record.get('attempt')})"
            return f"{stamp}retry failed: {_squash(str(record.get('finalError', '')))}"
        if kind == "agent_settled":
            return f"{stamp}settled" + (" (aborted)" if record.get("aborted") else "")
        if kind == "pool_end":
            self.finished = True
            return f"{stamp}pool end"
        return None


def follow(
    path: Path,
    emit: Callable[[str], None],
    clock: Callable[[], float],
    idle_timeout: float = IDLE_TIMEOUT,
) -> None:
    """Emit the rendered events of `path` as it grows, until `pool_end` or quiet for too long.

    A line is decoded once its newline has arrived — the pool may be mid-write, possibly in the
    middle of a multi-byte character, so the file is read as bytes and decoded line by line.

    Args:
        path: the events file.
        emit: called with each piece of rendered text.
        clock: seconds elapsed since the viewer started.
        idle_timeout: seconds without any new data after which the follow ends.
    """
    renderer = EventRenderer(clock)
    pending = b""
    last_growth = time.monotonic()
    with path.open("rb") as f:
        while not renderer.finished:
            chunk = f.read()
            if not chunk:
                if time.monotonic() - last_growth > idle_timeout:
                    return
                time.sleep(_POLL_SECONDS)
                continue
            last_growth = time.monotonic()
            *complete, pending = (pending + chunk).split(b"\n")
            for raw in complete:
                try:
                    record = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if isinstance(record, dict):
                    text = renderer.feed(record)
                    if text:
                        emit(text)
                if renderer.finished:
                    return


def main(argv: list[str] | None = None) -> int:
    """Wait for the events file, then follow it.

    Args:
        argv: the arguments; defaults to `sys.argv[1:]`.

    Returns:
        0 once the run ended or went quiet, 1 if the file never appeared.
    """
    ap = argparse.ArgumentParser(description="Follow a pool run, one line per event.")
    ap.add_argument("events_file", type=Path)
    ap.add_argument("--idle-timeout", type=float, default=IDLE_TIMEOUT)
    args = ap.parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")

    started = time.monotonic()
    while not args.events_file.exists():
        if time.monotonic() - started > args.idle_timeout:
            print(
                f"no events file {args.events_file} within {args.idle_timeout:g}s",
                file=sys.stderr,
            )
            return 1
        time.sleep(_POLL_SECONDS)

    def emit(text: str) -> None:
        """Print `text` as is and flush, so a streamed delta shows at once."""
        sys.stdout.write(text)
        sys.stdout.flush()

    follow(
        args.events_file, emit, lambda: time.monotonic() - started, args.idle_timeout
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
