"""The view files pc-models' `pi-view` mod draws in a running tool call's transcript row.

The mod names one directory per call, `<config dir>/pc-models/pi-runs/<tool_use_id>`, and passes it
as the tool's `events` argument. `open_run` accepts only that shape and creates the directory
private (0700), so a value the model wrote itself never makes the server write elsewhere. Each
`pi` stream then gets one `view-<slot>.txt`: a header and the last `KEEP_LINES` lines that
`pi_events_view.EventRenderer` makes of its events, rewritten whole (temp file, then rename) at most
every `WRITE_EVERY_S`, so the file stays small and a reader never sees half of it.

An `offline` run gets a `ProgressView`: the count of characters received, never the text — the
row is drawn on every attached surface, remote ones included, and `offline` never lets its answer
leave the machine.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pi_events_view

KEEP_LINES = 8
LINE_CHARS = 200
WRITE_EVERY_S = 0.2
MAX_AGE_S = 86_400
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def runs_dir() -> Path:
    """The directory every run directory lives in, under the Claude Code config directory."""
    config = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(config) / "pc-models" / "pi-runs"


def _prune(root: Path) -> None:
    """Best-effort: remove the run directories older than `MAX_AGE_S`."""
    limit = time.time() - MAX_AGE_S
    for run in root.iterdir():
        try:
            if run.is_dir() and not run.is_symlink() and run.stat().st_mtime < limit:
                shutil.rmtree(run)
        except OSError:
            pass


def open_run(events: str | None) -> Path | None:
    """Create the run directory `events` names, or None when it is not one the mod would name.

    Args:
        events: the tool's `events` argument.

    Returns:
        The new, private directory; None for no value, a path outside `runs_dir()`, a name
        outside `[A-Za-z0-9_-]{1,128}`, an existing entry (a link included) or any OS error.
    """
    if not events:
        return None
    root = runs_dir()
    run = Path(events)
    if run.parent != root or not _ID_RE.fullmatch(run.name):
        return None
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _prune(root)
        run.mkdir(mode=0o700)
    except OSError:
        return None
    return run


def _clip(line: str) -> str:
    """`line` cut to its last `LINE_CHARS` characters: a line still being written shows its end."""
    return line if len(line) <= LINE_CHARS else "…" + line[-(LINE_CHARS - 1) :]


class _ViewFile:
    """A header and lines, rewritten whole into one file, at most every `WRITE_EVERY_S`."""

    def __init__(self, path: Path, header: str, clock: Callable[[], float]) -> None:
        self._path = path
        self._header = pi_events_view._plain(header)
        self._clock = clock
        self._written_at: float | None = None
        self._lock = threading.Lock()

    def lines(self) -> list[str]:
        """The lines to show under the header, oldest first."""
        raise NotImplementedError

    def save(self, force: bool = False) -> None:
        """Best-effort: rewrite the file, unless the last write is younger than `WRITE_EVERY_S`.

        Args:
            force: write whatever the time since the last write.
        """
        now = self._clock()
        if (
            not force
            and self._written_at is not None
            and now - self._written_at < WRITE_EVERY_S
        ):
            return
        self._written_at = now
        shown = [self._header, *self.lines()][-KEEP_LINES:]
        if len(shown) == KEEP_LINES:
            shown[0] = self._header
        text = "".join(_clip(line) + "\n" for line in shown)
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            pass


class LiveView(_ViewFile):
    """One `pi` stream's view: a text sink `call_cli` and `run_pool` copy raw event lines into."""

    def __init__(
        self,
        run: Path,
        slot: int,
        header: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start the view and write its header.

        Args:
            run: the directory `open_run` created.
            slot: the stream's number, which names its file.
            header: the first line, always shown.
            clock: seconds, read for the stamps and the write interval.
        """
        super().__init__(run / f"view-{slot}.txt", header, clock)
        start = clock()
        self._renderer = pi_events_view.EventRenderer(lambda: clock() - start)
        self._done: list[str] = []
        self._current = ""
        self._pending = ""
        self._thinking_at: int | None = None
        self._thinking_chars = 0
        self._thinking_stamp = ""
        self.save(force=True)

    def lines(self) -> list[str]:
        """The rendered lines, the one being written last."""
        return self._done + ([self._current] if self._current else [])

    def write(self, text: str) -> int:
        """Take raw event lines, possibly cut anywhere; render each complete one.

        Args:
            text: the next characters of the events stream.

        Returns:
            The number of characters taken, as a text file's `write` does.
        """
        with self._lock:
            self._pending += text
            *complete, self._pending = self._pending.split("\n")
            for line in complete:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    self._feed(record)
            self.save()
        return len(text)

    def flush(self) -> None:
        """Write the file if the interval has passed."""
        with self._lock:
            self.save()

    def close(self) -> None:
        """Write the file whatever the interval."""
        with self._lock:
            self.save(force=True)

    def _feed(self, record: dict[str, Any]) -> None:
        """Render one event; thinking shows as one line counting its characters."""
        update = record.get("assistantMessageEvent")
        kind = update.get("type") if isinstance(update, dict) else None
        if kind == "thinking_delta" and isinstance(update, dict):
            self._thinking_chars += len(str(update.get("delta", "")))
            if self._thinking_at is not None:
                self._done[self._thinking_at] = self._thinking_line()
            return
        out = self._renderer.feed(record)
        if kind == "thinking_start":
            self._push(out.removesuffix("\n").removesuffix("thinking"))
            self._thinking_stamp = self._current
            self._current = ""
            self._thinking_chars = 0
            self._thinking_at = len(self._done)
            self._done.append(self._thinking_line())
            return
        self._push(out)

    def _thinking_line(self) -> str:
        """The thinking line, with the characters counted so far."""
        return f"{self._thinking_stamp}thinking · {self._thinking_chars} chars"

    def _push(self, out: str) -> None:
        """Append rendered text, closing a line at each newline."""
        *closed, self._current = (self._current + out).split("\n")
        self._done.extend(closed)
        overflow = len(self._done) - KEEP_LINES
        if overflow > 0:
            del self._done[:overflow]
            if self._thinking_at is not None:
                self._thinking_at = (
                    self._thinking_at - overflow
                    if self._thinking_at >= overflow
                    else None
                )


class ProgressView(_ViewFile):
    """An `offline` run's view: how many characters the model has sent, never what they are."""

    def __init__(
        self, run: Path, header: str, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Start the view and write its header.

        Args:
            run: the directory `open_run` created.
            header: the first line, always shown.
            clock: seconds, read for the write interval.
        """
        super().__init__(run / "view-1.txt", header, clock)
        self._chars = 0
        self.save(force=True)

    def lines(self) -> list[str]:
        """The one count line, once something was received."""
        return [f"{self._chars} chars received"] if self._chars else []

    def __call__(self, chunk: str) -> None:
        """Count a chunk of the answer.

        Args:
            chunk: characters `pi` printed.
        """
        with self._lock:
            self._chars += len(chunk)
            self.save()

    def close(self) -> None:
        """Write the file whatever the interval."""
        with self._lock:
            self.save(force=True)
