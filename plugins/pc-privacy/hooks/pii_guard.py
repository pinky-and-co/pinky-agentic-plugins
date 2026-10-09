#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["presidio-analyzer", "presidio-anonymizer", "openpyxl", "fr-core-news-md @ https://github.com/explosion/spacy-models/releases/download/fr_core_news_md-3.8.0/fr_core_news_md-3.8.0-py3-none-any.whl"]
# ///
"""PreToolUse / Read — alert before a native `Read` of a dataset that holds personal data.

Reuses ``anonymize.detect_findings`` as-is (same Presidio + French NLP + custom FR recognizers as
the `pc-privacy` MCP server) — no second deterministic detector. The decision is always ``ask``,
never ``deny``: the person sees which columns look sensitive and chooses. Reading as-is, refusing,
or refusing and running `anonymize_apply` afterwards are all their call; the hook only interrupts.

When ``ONE_DECISION_MODEL`` names a model served locally, ``decision_gate`` judges the file's state
of the file first: a ``pass`` ends the scan without Presidio, a ``block`` adds one line to the
Presidio report.
Unset, unreachable, or refusing the state, the gate is skipped with no message and Presidio
decides alone.

The same script also runs on ``UserPromptSubmit``: an ``@file`` typed in a prompt is inlined by
Claude Code without going through ``Read``, so ``decide_prompt`` scans the mentioned paths and
blocks the message (a prompt hook cannot ask).

Scope is decided by ``anonymize.detect_format`` (content-based, via `file`), not by extension:
config files (yml/toml/ini/env, a single JSON object, key:value-shaped text) and binaries (pdf,
images, docx...) are skipped without ever loading the NLP model — this hook runs on every `Read`,
so the cheap path has to stay cheap.

The agent's own scope is never scanned, whatever its content: a path under the resolved config
dir (``CLAUDE_CONFIG_DIR``, else ``~/.claude/``) or carrying a ``.claude`` directory component —
memory notes included — is skipped on both channels (a `Read` target and an ``@file`` prompt
mention). Blocking the agent on its own journal and settings is all friction, no protection, and
the repeated false positives end up getting the guard disabled.

The same script also answers the band mod (``hooks/pii-hold``): ``--verdict`` prints the guard's
judgement of a ``Read`` event as JSON, ``--anonymize <path>`` writes the anonymized copy and prints
its path. The guard stays the one source of the verdict; the mod only presents it.

Fail-open by design: a guard's own bug must never block a tool call.
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


# Appended to the listing when the decision model also judged the file personal.
DECISION_MODEL_LINE = (
    "A local decision model also judged this file to contain personal data."
)


def _gate_verdict(path: Path, fmt: str, anonymize) -> str | None:
    """Ask the local decision gate: ``"block"``, ``"pass"``, or None when it is unavailable.

    Args:
        path: The file to judge.
        fmt: Its format, as decided by ``anonymize.detect_format``.
        anonymize: The ``anonymize`` module.

    Returns:
        The gate's verdict, or None on any failure, including the gate module not importing.
    """
    try:
        import decision_gate

        return decision_gate.verdict(path, fmt, anonymize)
    except Exception:
        return None


def _is_excluded(path: Path) -> bool:
    """True for the agent's own scope, never scanned: a path with a ``.claude`` directory
    component, or under the resolved config dir — ``CLAUDE_CONFIG_DIR``, else ``~/.claude/``,
    which mechanically covers the default ``projects/<project>/memory`` silo. Lexical only (no
    filesystem call), so it stays cheaper than any format detection.

    Duplicated in ``privacy_egress.py`` (pc-secrets) rather than shared, so the plugins stay
    import-independent; ``tests/test_read_guard_parity.py`` keeps the two copies in step.

    Args:
        path: The candidate target, as it reached the hook.

    Returns:
        True when the file must not reach the scan.
    """
    expanded = path.expanduser()
    if ".claude" in expanded.parts:
        return True
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()
    return expanded.is_relative_to(config_dir)


def _scan(path: Path, anonymize) -> tuple[str, str] | None:
    """Classify and scan one file: ``("oversize" | "pii", message)``, or None when nothing to flag.

    The message carries column names and entity types only — never a sampled value.
    """
    if _is_excluded(path):
        return None  # the agent's own notes/config — never scanned
    if not path.is_file():
        return None  # let Read produce its own "no such file" error
    size = path.stat().st_size
    if size == 0:
        return None
    # Cheap classification first (reads the head of the file only). Config, code and binaries
    # never pay the NLP model load and never trigger an alert.
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
    # An anonymized copy keeps its column names: only its values can still betray a leak. Its
    # pseudonyms sit too close to the decision model's threshold, so the gate is not asked.
    anon_copy = ".anon." in path.name
    if anon_copy:
        kwargs["values_only"] = True
    judged = None if anon_copy else _gate_verdict(path, fmt, anonymize)
    if judged == "pass":
        return None
    cut: list[str] = []
    if fmt == "text":
        kwargs["text_max_lines"] = MAX_TEXT_LINES
        kwargs["truncated"] = cut.append
    findings = anonymize.detect_findings([str(path)], **kwargs)
    note = (
        f"Only the first {MAX_TEXT_LINES} lines of `{path.name}` were analyzed; the rest was "
        "NOT fully analyzed. It may contain personal data."
    )
    if not findings and judged == "block":
        return (
            "pii",
            f"A local decision model judged `{path.name}` to contain personal data, but no "
            "deterministic detector found a specific column — reading it puts it into the "
            "model's context (and whatever cloud sits behind it)."
            + (f"\n{note}" if cut else ""),
        )
    if not findings:
        return ("oversize", note) if cut else None
    listing = "\n".join(f"  - {f.column}: {f.reason}" for f in findings)
    return (
        "pii",
        f"Personal data detected in `{path.name}` — reading it puts it into the model's context "
        f"(and whatever cloud sits behind it):\n{listing}"
        + (f"\n{note}" if cut else "")
        + (f"\n{DECISION_MODEL_LINE}" if judged == "block" else ""),
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


def _columns(path: Path, anonymize) -> list[str]:
    """Names of the columns judged sensitive in `path`, empty for a skipped format or no finding."""
    if anonymize.detect_format(path) in anonymize._SKIP_FORMATS:
        return []
    kwargs: dict = {"text_max_lines": MAX_TEXT_LINES}
    if ".anon." in path.name:
        kwargs["values_only"] = True
    return [f.column for f in anonymize.detect_findings([str(path)], **kwargs)]


def verdict(event: dict, anonymize) -> dict:
    """The guard's judgement of a `Read` event, shaped for the band that offers the choices.

    Args:
        event: The PreToolUse event.
        anonymize: The ``anonymize`` module.

    Returns:
        dict: A dictionary with flagged status. Either ``{"flagged": False}`` or the flagged path
        with its kind (``pii`` or ``oversize``), its sensitive columns, and whether an anonymized
        copy can be offered (never for an oversize file, nor for a copy that is already anonymized).
    """
    file_path = (event.get("tool_input") or {}).get("file_path")
    if event.get("tool_name") != "Read" or not file_path:
        return {"flagged": False}
    path = Path(file_path)
    scanned = _scan(path, anonymize)
    if scanned is None:
        return {"flagged": False}
    kind = scanned[0]
    columns = _columns(path, anonymize) if kind == "pii" else []
    return {
        "flagged": True,
        "kind": kind,
        "path": str(path),
        "columns": columns,
        "anonymize": bool(columns) and ".anon." not in path.name,
    }


def make_copy(path: Path, anonymize) -> str | None:
    """Write the anonymized copy of `path` next to it.

    Args:
        path: The file to anonymize.
        anonymize: The ``anonymize`` module.

    Returns:
        The copy's path, or None when no column is flagged or nothing was masked.
    """
    columns = _columns(path, anonymize)
    if not columns:
        return None
    outcome = anonymize.apply_anonymization([str(path)], columns)
    copies = [dst for _, dst, masked in outcome.outputs if masked]
    return str(path.parent / copies[0]) if copies else None


def main(argv: list[str]) -> int:
    """Run the hook, or one of the mod's helper modes (``--verdict``, ``--anonymize <path>``).

    Args:
        argv: The arguments after the script name.

    Returns:
        The process exit code; 1 when a helper mode has nothing to answer.
    """
    if argv[:1] == ["--check"]:
        return check()
    if argv[:1] == ["--verdict"] or (argv[:1] == ["--anonymize"] and len(argv) == 2):
        try:
            anonymize = _import_anonymize()
            if argv[0] == "--verdict":
                print(
                    json.dumps(verdict(json.loads(sys.stdin.read() or "{}"), anonymize))
                )
                return 0
            copy = make_copy(Path(argv[1]), anonymize)
        except Exception:  # the mod treats no answer as « no band » or « no copy »
            return 1
        if copy is None:
            return 1
        print(json.dumps({"copy": copy}))
        return 0
    return run_hook()


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
    sys.exit(main(sys.argv[1:]))
