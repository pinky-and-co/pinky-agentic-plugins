#!/usr/bin/env python3
"""UserPromptSubmit — per-turn reinforcement for a custom output style.

Source : smixs/awesome-claude-output-styles, hooks/style-reminder.sh · License : MIT
Ported from shell to Python for consistency with this plugin's other hooks (Windows-safe stdlib,
no `sh`/`sed` dependency) — logic unchanged.

Claude Code injects a per-turn reminder ("<Name> output style is active. Remember to follow the
specific guidelines for this style.") ONLY for its built-in styles (Proactive, Explanatory,
Learning). A custom style is placed in the system prompt once and never reinforced, so its voice
fades in long sessions. This hook emits the same reminder line for whatever custom style is active,
closing that gap — it never re-injects the style's own content, only its name.

Python 3.12+, stdlib only. A hook failure must never block a turn → unexpected errors exit 0.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def active_style() -> str:
    """The `outputStyle` value from `~/.claude/settings.json`, or "" if unset/unreadable."""
    settings_path = (
        Path(os.environ.get("CLAUDE_DIR", Path.home() / ".claude")) / "settings.json"
    )
    try:
        return (
            json.loads(settings_path.read_text(encoding="utf-8")).get("outputStyle")
            or ""
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return ""


def custom_style_names() -> frozenset[str]:
    """Every `name:` frontmatter value found under `~/.claude/output-styles/*.md`.

    A built-in style (Concise, Proactive, ...) has no file here — Claude Code ships its own
    per-turn reminder for those. Only a name matching a file in this directory is ours to remind.
    """
    styles_dir = (
        Path(os.environ.get("CLAUDE_DIR", Path.home() / ".claude")) / "output-styles"
    )
    names: set[str] = set()
    for path in styles_dir.glob("*.md") if styles_dir.is_dir() else ():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("name:"):
                    names.add(line.split(":", 1)[1].strip())
                    break
        except OSError:
            continue
    return frozenset(names)


def reminder() -> str | None:
    """The reminder line for the active custom style, or None if built-in/unset."""
    style = active_style()
    if not style or style not in custom_style_names():
        return None
    return (
        f"{style} output style is active. "
        "Remember to follow the specific guidelines for this style."
    )


def run_hook() -> int:
    try:
        line = reminder()
    except Exception:  # a guard must never block a turn on its own bug
        return 0
    if line is not None:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(run_hook())
