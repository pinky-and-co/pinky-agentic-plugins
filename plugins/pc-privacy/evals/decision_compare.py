#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["presidio-analyzer", "presidio-anonymizer", "openpyxl", "fr-core-news-md @ https://github.com/explosion/spacy-models/releases/download/fr_core_news_md-3.8.0/fr_core_news_md-3.8.0-py3-none-any.whl"]
# ///
"""Compare the `pii_guard` decision with and without its decision-model gate, one verdict per file.

    uv run --script decision_compare.py FILE[:block|:pass] [...]

Needs a running local mlx-serve with `ONE_DECISION_MODEL` set (and `ONE_DECISION_URL` when it is
not on the default port): it calls the real gate. Per file it prints the label, the Presidio-only
decision, the gate's probability of personal data (or `unavailable`) and the decision of the guard
with the gate active, then the false alarms and misses of each.

Label a file `:block` (must be blocked) or `:pass` (must not) to get error counts.

Prints file names, labels, probabilities and decisions only, never a value.

What to look at:
  - `missed`      : labelled :block and judged pass -> a leak, the costly error
  - `false alarm` : labelled :pass and judged block -> a needless interruption
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "mcp"))
sys.path.insert(0, str(_HERE.parent / "hooks"))
import anonymize  # noqa: E402
import decision_gate  # noqa: E402
import pii_guard  # noqa: E402


@dataclass(frozen=True)
class Case:
    """A file to judge, with the decision it must get when the person labelled it."""

    path: Path
    expect: str | None  # "block", "pass" or None (unlabelled)


def _parse_case(arg: str) -> Case:
    """Split `path[:block|:pass]` into a Case.

    Args:
        arg: A file path, optionally suffixed with its expected decision.

    Returns:
        The case; the label is None when the suffix is absent.
    """
    for label in ("block", "pass"):
        if arg.endswith(f":{label}"):
            return Case(Path(arg[: -len(label) - 1]), label)
    return Case(Path(arg), None)


def _decision(path: Path) -> str:
    """What `pii_guard` does on a Read of this file: ask (block) or let it through.

    Args:
        path: The file.

    Returns:
        "block" when the guard would ask the person, "pass" otherwise.
    """
    return "block" if pii_guard._scan(path, anonymize) is not None else "pass"


def _presidio_only(path: Path) -> str:
    """The guard's decision with the decision-model gate switched off.

    Args:
        path: The file.

    Returns:
        "block" or "pass".
    """
    model = os.environ.pop(decision_gate.MODEL_ENV, None)
    try:
        return _decision(path)
    finally:
        if model is not None:
            os.environ[decision_gate.MODEL_ENV] = model


def _errors(verdicts: dict[Path, str], cases: list[Case]) -> str:
    """Count the labelled files a detector got wrong.

    Args:
        verdicts: File -> verdict of one detector.
        cases: The files and their labels.

    Returns:
        A one-line summary of the correct, missed and false-alarm counts.
    """
    labelled = [c for c in cases if c.expect]
    ok = sum(verdicts[c.path] == c.expect for c in labelled)
    missed = sum(c.expect == "block" and verdicts[c.path] != "block" for c in labelled)
    alarms = sum(c.expect == "pass" and verdicts[c.path] != "pass" for c in labelled)
    return f"{ok}/{len(labelled)} correct, {missed} missed, {alarms} false alarm"


def main() -> int:
    """Judge each file with Presidio alone and with the decision-model gate, then print both.

    Returns:
        0 on success, 2 when no file can be used or the gate is not configured.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()

    if not os.environ.get(decision_gate.MODEL_ENV):
        print(f"{decision_gate.MODEL_ENV} is not set: the gate would never answer")
        return 2
    cases = [_parse_case(a) for a in args.files]
    formats = {c.path: anonymize.detect_format(c.path) for c in cases}
    cases = [c for c in cases if formats[c.path] not in anonymize._SKIP_FORMATS]
    if not cases:
        print("every file is config, code or binary: the guard never scans those")
        return 2

    presidio = {c.path: _presidio_only(c.path) for c in cases}
    probabilities = {
        c.path: decision_gate.probability(c.path, formats[c.path], anonymize)
        for c in cases
    }
    principal = {c.path: _decision(c.path) for c in cases}

    print(f"{'file':<48} {'label':<6} {'presidio':<9} {'P(A)':<12} tev-principal")
    for c in cases:
        p = probabilities[c.path]
        shown = "unavailable" if p is None else f"{p:.3f}"
        print(
            f"{c.path.name:<48} {(c.expect or '-'):<6} {presidio[c.path]:<9} "
            f"{shown:<12} {principal[c.path]}"
        )
    if any(c.expect for c in cases):
        print()
        print(f"presidio alone: {_errors(presidio, cases)}")
        print(f"tev-principal: {_errors(principal, cases)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
