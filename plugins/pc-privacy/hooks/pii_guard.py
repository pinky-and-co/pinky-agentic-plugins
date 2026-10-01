#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["presidio-analyzer", "presidio-anonymizer", "openpyxl", "fr-core-news-md @ https://github.com/explosion/spacy-models/releases/download/fr_core_news_md-3.8.0/fr_core_news_md-3.8.0-py3-none-any.whl"]
# ///
"""PreToolUse / Read — alert before a native `Read` of a dataset that holds personal data.

Reuses ``anonymize.detect_findings`` as-is (same Presidio + French NLP + custom FR recognizers as
the `pc-privacy` MCP server) — no second detector. The decision is always ``ask``, never ``deny``:
the person sees which columns look sensitive and chooses. Reading as-is, refusing, or refusing and
running `anonymize_apply` afterwards are all their call; the hook only interrupts.

The same script also runs on ``UserPromptSubmit``: an ``@file`` typed in a prompt is inlined by
Claude Code without going through ``Read``, so ``decide_prompt`` scans the mentioned paths and
blocks the message (a prompt hook cannot ask).

Scope is decided by ``anonymize.detect_format`` (content-based, via `file`), not by extension:
config files (yml/toml/ini/env, a single JSON object, key:value-shaped text) and binaries (pdf,
images, docx...) are skipped without ever loading the NLP model — this hook runs on every `Read`,
so the cheap path has to stay cheap.

Fail-open by design (same rule as `read_cost.py`): a guard's own bug must never block a tool call.
The flip side is that a broken guard is silent, so ``pii_guard.py --check`` exists — run it after
install and after any dependency change.

Python 3.12+. Run via ``uv run --script`` so Presidio/openpyxl resolve from the inline metadata.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# Above this size the scan is not attempted (column sampling still loads the whole file, and the
# positional fallback runs Presidio on every line). The person is told the file was NOT analyzed,
# rather than the hook silently allowing it or hanging until the hook timeout.
MAX_BYTES = int(os.environ.get("PII_GUARD_MAX_MB", "100")) * 1024 * 1024

# A file scanned line by line (a log, a positional export) costs ~3 ms a line with no sampling, so
# the byte cap alone would let a few MB run into the hook timeout. Past this many lines only the
# first ones are read and the person is told the rest was NOT analyzed.
MAX_TEXT_LINES = int(os.environ.get("PII_GUARD_MAX_TEXT_LINES", "3000"))


def _import_anonymize():
    """Import `anonymize.py` from the plugin, wherever it sits relative to this hook."""
    here = Path(__file__).resolve().parent
    override = os.environ.get("PC_PRIVACY_DIR")
    candidates = [Path(override)] if override else []
    candidates += [here, here.parent, here.parent / "mcp"]
    for d in candidates:
        if (d / "anonymize.py").is_file():
            sys.path.insert(0, str(d))
            import anonymize

            return anonymize
    raise ImportError(
        "anonymize.py not found — set PC_PRIVACY_DIR to the directory that contains it"
    )


def _ask(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


def _scan(path: Path, anonymize) -> tuple[str, str] | None:
    """Classify and scan one file: ``("oversize" | "pii", message)``, or None when nothing to flag.

    The message carries column names and entity types only — never a sampled value.
    """
    if not path.is_file():
        return None  # let Read produce its own "no such file" error
    size = path.stat().st_size
    if size == 0:
        return None
    # Cheap classification first (reads the head of the file only). Config/code/binary never pay
    # the NLP model load and never trigger an alert.
    fmt = anonymize.detect_format(path)
    if fmt in anonymize._SKIP_FORMATS:
        return None
    if size > MAX_BYTES:
        return (
            "oversize",
            f"`{path.name}` is {size // (1024 * 1024)} MB — above the {MAX_BYTES // (1024 * 1024)} MB "
            "limit of the PII scan, so it was NOT analyzed. It may contain personal data.",
        )
    kwargs: dict = {}
    # An anonymized copy keeps its column names: only its values can still betray a leak.
    if ".anon." in path.name:
        kwargs["values_only"] = True
    cut: list[str] = []
    if fmt == "text":
        kwargs["text_max_lines"] = MAX_TEXT_LINES
        kwargs["truncated"] = cut.append
    findings = anonymize.detect_findings([str(path)], **kwargs)
    note = (
        f"Only the first {MAX_TEXT_LINES} lines of `{path.name}` were analyzed; the rest was "
        "NOT fully analyzed. It may contain personal data."
    )
    if not findings:
        return ("oversize", note) if cut else None
    listing = "\n".join(f"  - {f.column}: {f.reason}" for f in findings)
    return (
        "pii",
        f"Personal data detected in `{path.name}` — reading it puts it into the model's context "
        f"(and whatever cloud sits behind it):\n{listing}"
        + (f"\n{note}" if cut else ""),
    )


def decide(event: dict, anonymize) -> dict | None:
    """Return an `ask` decision if the file targeted by a `Read` looks like PII-bearing data."""
    if event.get("tool_name") != "Read":
        return None
    file_path = (event.get("tool_input") or {}).get("file_path")
    if not file_path:
        return None
    scanned = _scan(Path(file_path), anonymize)
    if scanned is None:
        return None
    kind, message = scanned
    if kind == "oversize":
        return _ask(f"{message} Confirm to read it anyway.")
    return _ask(
        f"{message}\n"
        "Confirm to read it as-is. To read an anonymized copy instead, refuse and run "
        "`anonymize_apply` from `pc-privacy`, then read the `.anon` file."
    )


# Typed by the person in their own prompt: lifts the `@file` block for that message. A model does
# not write the person's prompt, so it cannot forge it.
BYPASS_MARKER = "pii:ok"

# `@path` in a prompt: a token starting at a word boundary, up to the next whitespace.
_AT_MENTION = re.compile(r"(?<!\S)@(\S+)")


def decide_prompt(event: dict, anonymize) -> dict | None:
    """Block a prompt whose `@file` mentions include a PII-bearing file.

    Claude Code inlines an `@file` itself, never through the `Read` tool, so `decide` cannot see
    it. A hook can only block a prompt, not ask; the reason says how to get the `Read`
    confirmation back (mention the path without the `@`).
    """
    prompt = event.get("prompt") or ""
    if BYPASS_MARKER in prompt:
        return None
    cwd = Path(event.get("cwd") or ".")
    flagged: list[str] = []
    seen: set[Path] = set()
    for token in _AT_MENTION.findall(prompt):
        path = Path(token.rstrip(",;:)\"'")).expanduser()
        path = path if path.is_absolute() else cwd / path
        if path in seen:
            continue
        seen.add(path)
        scanned = _scan(path, anonymize)
        if scanned is not None:
            flagged.append(scanned[1])
    if not flagged:
        return None
    return {
        "decision": "block",
        "reason": "Message blocked — an `@file` is inlined without any confirmation.\n"
        + "\n".join(flagged)
        + "\nMention the path without the @ to get the read confirmation, run "
        "`anonymize_apply` from `pc-privacy` and attach the `.anon` file, or add "
        f"`{BYPASS_MARKER}` to the message to send it anyway.",
    }


def run_hook() -> int:
    """Read the PreToolUse event from stdin, emit a decision when one applies, never raise."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0
    try:
        handler = (
            decide_prompt
            if event.get("hook_event_name") == "UserPromptSubmit"
            else decide
        )
        decision = handler(event, _import_anonymize())
    except Exception as exc:  # a guard must never block a tool call on its own bug
        print(
            f"[pii_guard] disabled for this call: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 0
    if decision is not None:
        print(json.dumps(decision, ensure_ascii=False))
    return 0


def check() -> int:
    """Self-test — a fail-open guard that is broken looks exactly like a guard that is fine."""
    try:
        anonymize = _import_anonymize()
        anonymize._build_analyzer()
    except Exception as exc:
        print(
            f"[pii_guard --check] NOT WORKING: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(
        "[pii_guard --check] OK — anonymize.py importable, Presidio + spaCy model load."
    )
    return 0


if __name__ == "__main__":
    sys.exit(check() if "--check" in sys.argv[1:] else run_hook())
