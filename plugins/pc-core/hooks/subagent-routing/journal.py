#!/usr/bin/env python3
"""Append one routing decision to `~/.claude/pinky/plugin-state/pc-core/subagent-routing.jsonl`.

Usage: `journal.py < decision.json`, where the JSON object carries `prompt`, `agent_type`,
`requested_model`, `requested_effort`, `category`, `applied_model`, `applied_effort`,
`classifier_model`, `error` (the classifier's failure, empty when it worked) and `reason`. Only those fields are written (anything else on stdin, such as
an environment or a key, is dropped), the prompt is cut to its first 200 characters, every other
value to 100, and control characters become spaces, so a line stays under 4000 characters by construction.

`reason` is one of `classified`, `timeout`, `no-band`, `user-choice`; any other
value exits 2 and writes nothing.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

PROMPT_CHARS = 200
VALUE_CHARS = 100
FIELDS = (
    "agent_type",
    "requested_model",
    "requested_effort",
    "category",
    "applied_model",
    "applied_effort",
    "classifier_model",
    "error",
)
CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
REASON = re.compile(r"classified|timeout|no-band|user-choice")


def flat(value: object) -> str:
    """Turn a value into text with control characters replaced by spaces.

    Args:
        value: any decoded JSON value.

    Returns:
        The one-line text.
    """
    return CONTROL.sub(" ", str(value))


def build_entry(decision: dict) -> dict[str, str]:
    """Keep the journal fields of a decision, cut to their caps.

    Args:
        decision: the decoded JSON object.

    Returns:
        The entry, with a UTC `ts`.

    Raises:
        ValueError: when `reason` is not one of the allowed values.
    """
    reason = flat(decision.get("reason", ""))
    if not REASON.fullmatch(reason):
        raise ValueError(f"unknown reason {reason!r}")
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    entry["prompt"] = flat(decision.get("prompt", ""))[:PROMPT_CHARS]
    for name in FIELDS:
        entry[name] = flat(decision.get(name, ""))[:VALUE_CHARS]
    entry["reason"] = reason[:VALUE_CHARS]
    return entry


def append(decision: dict, path: Path | None = None) -> None:
    """Append one JSON line for `decision`, creating the directory.

    A default-path write also removes `~/.claude/pc-core/` when that directory exists
    (best-effort): nothing reads it, its journal history is abandoned.

    Args:
        decision: the decoded JSON object.
        path: the journal file, under `~/.claude/pinky/plugin-state/pc-core/` by default.

    Raises:
        ValueError: when `reason` is not one of the allowed values.
    """
    line = json.dumps(build_entry(decision), ensure_ascii=False)
    if path is None:
        legacy = Path.home() / ".claude" / "pc-core"
        if legacy.is_dir():
            shutil.rmtree(legacy, ignore_errors=True)
        path = (
            Path.home()
            / ".claude"
            / "pinky"
            / "plugin-state"
            / "pc-core"
            / "subagent-routing.jsonl"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main() -> int:
    """CLI entrypoint: one decision as JSON on stdin.

    Returns:
        0 when the line is written, 2 on an unreadable decision or an unknown reason.
    """
    try:
        decision = json.load(sys.stdin)
        if not isinstance(decision, dict):
            raise ValueError("not an object")
        append(decision)
    except ValueError as exc:
        print(f"journal: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
