#!/usr/bin/env python3
"""inject_policies — PreToolUse hook that injects language / workflow
policies from .claude/settings.pinky.json right before the tool call that
produces the relevant artefact.

Why not CLAUDE.md: CLAUDE.md is prose meant for a model to read; parsing
it with sed/regex is fragile against reformatting. settings.pinky.json is data,
parsed once with json.load, no regex.

Why not settings.json: Claude Code writes to settings.json itself (the
/config UI, permanent "don't ask again" permission approvals). Mixing an
arbitrary custom top-level key into a file the tool itself rewrites is a
risk with no upside — settings.pinky.json is untouched by Claude Code, owned
entirely by this hook and by hand-editing.

Detection rules (which artefact_type(s) a tool call implicates) are kept
in this script rather than in settings.pinky.json — settings.pinky.json stays pure
"what language for what artefact" data, editable without touching logic.
Move DETECTORS to config later if it needs to vary per repo.

Registered as a PreToolUse (Bash|Write|Edit) hook in hooks/hooks.json.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from policy_lib import load_policies, resolve_repo_root  # noqa: E402

CODE_EXTENSIONS = {".py", ".sql", ".sh", ".ts", ".js", ".go", ".rs", ".java"}


def detect_artefact_types(tool_name: str, tool_input: dict) -> list[str]:
    """Return which artefact_type(s) a tool call implicates, or [] if none apply."""
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if re.search(r"\bgit\s+commit\b", command):
            return ["commit"]
        if re.search(r"\b(gh\s+pr|glab\s+mr)\s+create\b", command):
            return ["PR/MR"]
        return []

    if tool_name in ("Write", "Edit"):
        path = tool_input.get("file_path", "")
        p = Path(path)
        parts_lower = "/".join(p.parts).lower()

        if p.suffix in CODE_EXTENSIONS:
            # co-occurring artefact types within the same file
            return ["code_identifiers", "docstrings", "inline_comments"]
        if any(
            seg in parts_lower
            for seg in ("docs/reference", "docs/how-to", "docs/tutorial")
        ):
            return ["public_docs"]
        if "docs/explanation" in parts_lower or "adr" in parts_lower:
            return ["explanation"]
        if any(
            seg in parts_lower
            for seg in ("docs/private", "docs/brainstorm", "docs/pitch")
        ):
            return ["private_docs"]
        return []

    return []


def wants_branch_workflow(tool_name: str, tool_input: dict) -> bool:
    """Return True if this tool call is a PR/MR creation or a tracked push (branch policy relevant)."""
    if tool_name != "Bash":
        return False
    command = tool_input.get("command", "")
    return bool(
        re.search(r"\b(gh\s+pr|glab\s+mr)\s+create\b", command)
        or re.search(r"\bgit\s+push\s+-u\b", command)
    )


def reverse_language_map(policies: dict) -> dict[str, str]:
    """Invert `policies["language"]` (lang -> [artefact_types]) into artefact_type -> lang."""
    reverse = {}
    for lang, artefact_types in policies.get("language", {}).items():
        for artefact_type in artefact_types:
            reverse[artefact_type] = lang
    return reverse


def main() -> None:
    """Read a PreToolUse payload from stdin and print an additionalContext injection, or nothing."""
    import os

    payload = json.load(sys.stdin)
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    artefact_types = detect_artefact_types(tool_name, tool_input)
    branch_workflow_relevant = wants_branch_workflow(tool_name, tool_input)

    if not artefact_types and not branch_workflow_relevant:
        return  # nothing to say — silent, allow (no stdout = default allow)

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    repo_root = resolve_repo_root(project_dir, tool_input.get("file_path"))
    policies = load_policies(repo_root)
    if policies is None:
        return

    lines = []

    if artefact_types:
        lang_map = reverse_language_map(policies)
        by_lang: dict[str, list[str]] = {}
        for artefact_type in artefact_types:
            lang = lang_map.get(artefact_type)
            if lang:
                by_lang.setdefault(lang, []).append(artefact_type)
        if by_lang:
            parts = [f"{lang}: {', '.join(names)}" for lang, names in by_lang.items()]
            lines.append(
                "Language policy for this artefact — " + "; ".join(parts) + "."
            )

    if branch_workflow_relevant and policies.get("branch_workflow"):
        lines.append(f"Branch workflow: {policies['branch_workflow']}.")

    if not lines:
        return

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "additionalContext": " ".join(lines),
                }
            }
        )
    )


if __name__ == "__main__":
    main()
