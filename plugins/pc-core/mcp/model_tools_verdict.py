#!/usr/bin/env python3
"""Per pool entry, whether a tool loop works: a verdict read from the `pi` sessions already run.

For each entry of the pool this prints `true`, `false` or `inconnu`, the evidence behind it, and
the `tools:` value that would follow. It writes nothing — not to the pool configuration, not
anywhere: the verdict is a proposal and the pool belongs to whoever edits it.

A run is one session file in which tools were offered (the system message lists some). Its steps
are the assistant messages that stopped on a tool use. A run counts as:

- success — the last assistant message stopped normally after at least 2 steps, with an answer that
  does not end on a closing tool-call tag;
- failure — after at least 2 steps, either the answer ends on a closing tool-call tag (a call
  written as text) or the run ended on a 429.

Thresholds: `true` for at least 2 successes and no tool call written as text; `false` for at least
2 failures and no success; `inconnu` otherwise. An entry with no run of its own takes the verdict of
its provider (all that provider's runs pooled); an entry with runs keeps its own verdict, even when
it is `inconnu`.

Usage:
    model_tools_verdict.py [--sessions DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal, NamedTuple

sys.path.insert(0, str(Path(__file__).parent))
import model_adversarial as ma  # noqa: E402

Verdict = Literal["true", "false", "inconnu"]
Outcome = Literal["success", "text", "429"]

MIN_STEPS = 2
MIN_RUNS = 2


class Run(NamedTuple):
    """One session: the model that ran it and how its tool loop ended."""

    provider: str
    model: str
    outcome: Outcome | None


class EntryVerdict(NamedTuple):
    """One table row: the entry, its verdict, and what it rests on."""

    name: str
    verdict: Verdict
    runs: int
    source: Literal["entry", "provider", "none"]


def default_sessions_dir() -> Path:
    """pi's session store."""
    return Path.home() / ".pi" / "agent" / "sessions"


def _text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(
        c.get("text", "")
        for c in content or []
        if isinstance(c, dict) and c.get("type") == "text"
    )


def read_run(path: Path) -> Run | None:
    """The `Run` of one session file; None when no tools were offered or nothing is classifiable.

    Args:
        path: a pi session `.jsonl`.

    Returns:
        The run, whose `outcome` is None when the loop neither failed nor succeeded after at least
        `MIN_STEPS` steps (too short to count, or ended on another error).
    """
    tools_offered = False
    steps = 0
    last: dict[str, Any] | None = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            sections = message.get("sections")
            tools_offered = isinstance(sections, dict) and bool(sections.get("tools"))
        elif role == "assistant":
            last = message
            if message.get("stopReason") == "toolUse":
                steps += 1
    if not tools_offered or last is None:
        return None
    provider, model = last.get("provider"), last.get("model")
    if not isinstance(provider, str) or not isinstance(model, str):
        return None
    outcome: Outcome | None = None
    if steps >= MIN_STEPS:
        stop = last.get("stopReason")
        if stop == "error" and ma.is_rate_limited(str(last.get("errorMessage", ""))):
            outcome = "429"
        elif stop == "stop":
            outcome = (
                "text" if ma._TOOL_CALL_TAIL_RE.search(_text_of(last)) else "success"
            )
    return Run(provider, model, outcome)


def read_runs(sessions_dir: Path) -> list[Run]:
    """Every classifiable-or-not tool run under `sessions_dir`."""
    runs = (read_run(p) for p in sorted(sessions_dir.glob("*/*.jsonl")))
    return [r for r in runs if r is not None]


def verdict_of(runs: list[Run]) -> Verdict:
    """The verdict of a set of runs, by the thresholds of the module docstring."""
    outcomes = [r.outcome for r in runs]
    successes = outcomes.count("success")
    failures = outcomes.count("text") + outcomes.count("429")
    if successes >= MIN_RUNS and "text" not in outcomes:
        return "true"
    if failures >= MIN_RUNS and successes == 0:
        return "false"
    return "inconnu"


def entry_verdicts(pool: list[dict[str, Any]], runs: list[Run]) -> list[EntryVerdict]:
    """One `EntryVerdict` per `kind: "cli"` pool entry, in pool order."""
    rows: list[EntryVerdict] = []
    for entry in pool:
        if entry.get("kind") != "cli":
            continue
        own = [
            r
            for r in runs
            if (r.provider, r.model) == (entry.get("provider"), entry.get("model"))
        ]
        if own:
            rows.append(EntryVerdict(entry["name"], verdict_of(own), len(own), "entry"))
            continue
        theirs = [r for r in runs if r.provider == entry.get("provider")]
        if theirs:
            rows.append(
                EntryVerdict(entry["name"], verdict_of(theirs), len(theirs), "provider")
            )
        else:
            rows.append(EntryVerdict(entry["name"], "inconnu", 0, "none"))
    return rows


def render(rows: list[EntryVerdict]) -> str:
    """The table: entry, verdict, run count and where the evidence comes from."""
    width = max((len(r.name) for r in rows), default=0)
    lines = ["entry verdict runs source"]
    lines += [
        f"{r.name.ljust(width)}  {r.verdict:<7}  {r.runs:>4}  {r.source}" for r in rows
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Print the verdict table for the configured pool; write nothing."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sessions", type=Path, default=None)
    args = ap.parse_args(argv)
    sessions = args.sessions or default_sessions_dir()
    print(render(entry_verdicts(ma.load_pool(), read_runs(sessions))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
