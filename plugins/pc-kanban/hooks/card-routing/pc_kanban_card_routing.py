#!/usr/bin/env python3
"""Write a card's `model:` and `effort:` from the pc-core routing classifier.

    pc_kanban_card_routing.py <card.md>

Runs `pc-core`'s `subagent-routing/classify.py` as a subprocess (the card body, frontmatter
stripped, on stdin) and writes the category's model and effort into the card's frontmatter,
replacing any value already there. A classifier failure writes nothing and exits 1: the grid
default it falls back to is not a decision about this card. Python 3.11+, stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CLASSIFIER = Path("hooks") / "subagent-routing" / "classify.py"
BOM = "\ufeff"
ROOT_ENV = "PC_CORE_ROOT"
MODELS = ("haiku", "sonnet", "opus")
EFFORTS = ("low", "medium", "high")
INSTALLED_PLUGINS = Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def core_root_candidates() -> list[Path]:
    """List where pc-core may sit, most trustworthy first.

    A plugin cannot import another's module, so the classifier is found by path: an explicit
    override (`PC_CORE_ROOT`, alone when set); the sibling `plugins/pc-core` of a working-tree install; the `pc-core/<version>`
    sibling of a cache install; the `installPath` Claude Code records for `pc-core`.

    Returns:
        The candidate pc-core roots.
    """
    if os.environ.get(ROOT_ENV):
        return [Path(os.environ[ROOT_ENV])]
    found: list[Path] = []
    found.append(PLUGIN_ROOT.parent / "pc-core")
    cache = PLUGIN_ROOT.parent.parent / "pc-core"
    if cache.is_dir():
        found += sorted(
            (d for d in cache.iterdir() if d.is_dir()),
            key=lambda d: d.name,
            reverse=True,
        )
    try:
        installed = json.loads(INSTALLED_PLUGINS.read_text(encoding="utf-8"))
        for plugin_id, entries in installed.get("plugins", {}).items():
            if plugin_id.split("@", 1)[0] == "pc-core":
                found += [
                    Path(e["installPath"]) for e in entries if e.get("installPath")
                ]
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return found


def find_classifier() -> Path | None:
    """Locate pc-core's classifier script.

    Returns:
        The first candidate root's `classify.py` that exists, else None.
    """
    for root in core_root_candidates():
        if (root / CLASSIFIER).is_file():
            return root / CLASSIFIER
    return None


def body_of(text: str) -> str:
    """Return a card without its frontmatter.

    Args:
        text: The whole card.

    Returns:
        The text after the closing `---`; the whole text when there is no frontmatter.
    """
    text = text.removeprefix(BOM)
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    return "".join(lines[end + 1 :]) if end else text


@dataclass(frozen=True)
class Routing:
    """What the classifier decided for a card, or why it did not."""

    model: str = ""
    effort: str = ""
    error: str = ""


def classify(text: str) -> Routing:
    """Classify a card body.

    Args:
        text: The whole card.

    Returns:
        The model and effort of the category; an `error` instead when the classifier is
        missing, fails, reports an `error`, or answers outside the grid's values.
    """
    script = find_classifier()
    if script is None:
        return Routing(error="pc-core's classifier was not found (set PC_CORE_ROOT)")
    try:
        run = subprocess.run(
            [sys.executable, str(script)],
            input=body_of(text),
            capture_output=True,
            text=True,
            check=False,
        )
        result = json.loads(run.stdout.strip().splitlines()[-1])
    except (OSError, IndexError, ValueError) as exc:
        return Routing(error=f"classifier unusable: {type(exc).__name__}: {exc}")
    if not isinstance(result, dict):
        return Routing(error="classifier unusable: not a JSON object")
    if result.get("error"):
        return Routing(error=f"classifier error: {result['error']}")
    if result.get("model") not in MODELS or result.get("effort") not in EFFORTS:
        return Routing(error=f"classifier answered outside the grid: {result}")
    return Routing(model=result["model"], effort=result["effort"])


def with_routing(text: str, model: str, effort: str) -> str:
    """Set `model:` and `effort:` in a card's frontmatter.

    Args:
        text: The whole card.
        model: The model alias.
        effort: The effort level.

    Returns:
        The card with both lines replaced in place, or added before the closing `---`. A
        leading BOM is dropped: the other card readers only see a frontmatter whose `---`
        opens the file, so a kept BOM would leave the written values unread.
    """
    text = text.removeprefix(BOM)
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return f"---\nmodel: {model}\neffort: {effort}\n---\n{text}"
    end = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == "---"), len(lines)
    )
    head = [ln for ln in lines[1:end] if not ln.startswith(("model:", "effort:"))]
    if head and not head[-1].endswith("\n"):
        head[-1] += "\n"
    tail = lines[end:] or ["---\n"]
    return "".join([lines[0], *head, f"model: {model}\n", f"effort: {effort}\n", *tail])


def write_routing(card: Path) -> Routing:
    """Classify a card and write the result into it.

    Args:
        card: The card file.

    Returns:
        The decision once written; its `error` set, with the card untouched, on a failure.
    """
    text = card.read_text(encoding="utf-8")
    decision = classify(text)
    if not decision.error:
        fresh = card.read_text(
            encoding="utf-8"
        )  # the card may change while classifying
        card.write_text(
            with_routing(fresh, decision.model, decision.effort), encoding="utf-8"
        )
    return decision


def main(argv: list[str]) -> int:
    """CLI entry point.

    Args:
        argv: `[card path]`.

    Returns:
        0 once written, 1 on a classifier failure, 2 on a usage error.
    """
    if len(argv) != 1 or not Path(argv[0]).is_file():
        print("usage: pc_kanban_card_routing.py <card.md>", file=sys.stderr)
        return 2
    decision = write_routing(Path(argv[0]))
    if decision.error:
        print(f"{argv[0]}: not written — {decision.error}", file=sys.stderr)
        return 1
    print(f"{argv[0]}: model {decision.model}, effort {decision.effort}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
