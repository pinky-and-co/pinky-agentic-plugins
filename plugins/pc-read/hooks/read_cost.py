#!/usr/bin/env python3
"""PreToolUse — warn before a native `Read` of an expensive format.

The problem isn't knowing how to read these formats — it's that reading them natively is
expensive. A `.pdf` or `.html` reads without error and lands whole in context, markup and all.
That is precisely what a "readable / unreadable" check misses: `.docx`/`.pptx` fail loudly and
get noticed; a PDF succeeds and costs.

`permissionDecision: "ask"`, never `deny` — a scanned or layout-dependent PDF genuinely needs a
native read; converting it would mutilate it. This hook interrupts and names the cheaper path
(`document-to-md`, `pc-read`); it does not decide in the caller's place.

The same script also answers the band mod (`hooks/read-cost-hold`): `--verdict` prints the hook's
judgement of a `Read` event as JSON (is the file costly, can `markitdown` convert it) and
`--target <path>` makes the private folder the conversion writes its Markdown into, and
`--discard <markdown>` removes that folder after a failed conversion. The hook stays
the one source of the verdict; the mod only presents it.

Python 3.12+, stdlib only. A hook failure must never block a tool call → unexpected errors exit 0.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

COSTLY_SUFFIXES = {".pdf", ".html", ".xlsx", ".docx", ".pptx"}
# Costly formats that can hold a dataset: the band offers the schema-and-first-rows mode for them.
DATA_SUFFIXES = {".xlsx"}


def target_suffix(tool_input: dict) -> str:
    """The lowercased suffix of the file a `Read` call targets, or `""` if none."""
    return Path(tool_input.get("file_path", "")).suffix.lower()


def decide(event: dict) -> dict | None:
    """Ask before a costly native read, else None (let it proceed)."""
    if event.get("tool_name") != "Read":
        return None
    suffix = target_suffix(event.get("tool_input") or {})
    if suffix not in COSTLY_SUFFIXES:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": (
                f"Reading a `{suffix}` natively loads the whole file (or its raw markup) into "
                "context. If this is a normal document — not a scanned or layout-dependent PDF — "
                "convert it first with the `document-to-md` skill (`pc-read`) instead. Confirm to "
                "read it natively anyway."
            ),
        }
    }


def verdict(event: dict) -> dict:
    """The hook's judgement of a `Read` event, for the band mod.

    Args:
        event: The PreToolUse event, as the hook receives it.

    Returns:
        A dict with ``flagged`` False alone, or, when flagged, the ``path``, ``markitdown`` (whether
        the converter is on the PATH) and ``data`` (whether the schema-and-first-rows mode applies).
    """
    if decide(event) is None:
        return {"flagged": False}
    return {
        "flagged": True,
        "path": event["tool_input"]["file_path"],
        "markitdown": shutil.which("markitdown") is not None,
        "data": target_suffix(event["tool_input"]) in DATA_SUFFIXES,
    }


def conversion_target(source: str) -> str:
    """A path for the Markdown of ``source``, unique to this conversion.

    The folder is created fresh and private (``mkdtemp``), so two sources sharing a file name in
    different folders never collide, and nobody else can plant a link where the converter writes.

    Args:
        source: The document to convert.

    Returns:
        The path of ``<name>.md`` inside a new folder; the file itself is not created.
    """
    folder = tempfile.mkdtemp(prefix="pc-read-")
    return str(Path(folder) / f"{Path(source).stem or 'document'}.md")


def discard_target(markdown: str) -> bool:
    """Remove the folder ``conversion_target`` made for ``markdown``, and nothing else.

    Args:
        markdown: A path returned by ``conversion_target``.

    Returns:
        True when the folder was removed; False when ``markdown`` does not sit in a
        ``pc-read-*`` folder directly under the temp directory, or the removal failed.
    """
    folder = Path(markdown).parent
    temp = Path(tempfile.gettempdir()).resolve()
    if (
        not folder.name.startswith("pc-read-")
        or folder.is_symlink()
        or not folder.is_dir()
        or folder.resolve().parent != temp
    ):
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return not folder.exists()


def run_hook() -> int:
    """Read the PreToolUse event from stdin, emit a decision when one applies, never raise."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0
    try:
        decision = decide(event)
    except Exception:  # a guard must never block a tool call on its own bug
        return 0
    if decision is not None:
        print(json.dumps(decision))
    return 0


def main(argv: list[str]) -> int:
    """Run the hook, or one of the mod's helper modes (``--verdict``, ``--target``, ``--discard``).

    Args:
        argv: The arguments after the script name.

    Returns:
        The exit code.
    """
    if argv[:1] == ["--verdict"]:
        try:
            event = json.loads(sys.stdin.read() or "{}")
            print(json.dumps(verdict(event)))
        except Exception:  # the mod treats no answer as "nothing to hold"
            return 1
        return 0
    if argv[:1] == ["--target"] and len(argv) == 2:
        try:
            print(json.dumps({"markdown": conversion_target(argv[1])}))
        except OSError:
            return 1
        return 0
    if argv[:1] == ["--discard"] and len(argv) == 2:
        return 0 if discard_target(argv[1]) else 1
    return run_hook()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
