#!/usr/bin/env python3
"""structure_guard — PreToolUse hook that denies a `Write` creating a file outside a repo's own
declared directory-structure convention.

The convention itself is data, never Python: a repo opts in and defines its own rule set under
`file_placement` in `.claude/settings.pinky.json`, resolved by the same walk-up as
`branch_workflow` (`policy_lib.load_policies`) — a repo that never sets the key sees no blocking,
ever (NFR-0023-23), and a repo can locally override an ancestor's value the same way any other key
is overridden (closest-declaring-repo wins). Adopting this on another repo, or adding a second
independent convention on this one, is a `settings.pinky.json` edit — this file never changes
(NFR-0023-24).

`file_placement` is a list of rules, each scoped to its own `root` (a path relative to the repo),
so a repo can declare N independent conventions at once. Each rule matches `<root>/<unit>/...` —
`<root>` needs a natural per-unit layer (a plugin name, a package name); a flat convention with
nothing playing that role does not fit this schema, since `root_dirs`/`root_files` bind at
`<unit>/`, never at `<root>/` itself:

    [
      {
        "root": "plugins",
        "root_dirs": ["hooks", "skills", "mcp"],
        "root_files": ["README.md"],
        "forbidden_dirs": ["commands"],
        "reserved_dirs": {"scripts": "pc-statusline"},
        "leaf_subdirs": {"skills": {"required_file": "SKILL.md", "subdirs": ["scripts"]}}
      }
    ]

`root_dirs`/`root_files` bound what may sit directly at `<unit>/`; `forbidden_dirs` names
directories never valid anywhere under `<unit>/`; `reserved_dirs` restricts a root-level directory
name to one specific `<unit>`; `leaf_subdirs` bounds what may sit directly under
`<unit>/<container>/<name>/` — a file there must be `required_file` (a flat file with no such
directory at all is equally rejected), a directory must be one of `subdirs`. Every key is
optional — an absent one simply does not constrain. A rule value that doesn't match its expected
shape (e.g. `forbidden_dirs` not a list) is ignored with a `stderr` warning, never trusted
half-parsed and never raised.

Rules are tried in declaration order; the first whose `root` is an ancestor of the write AND
yields a non-degenerate match (something under the unit segment, not just the unit itself) wins.
Two rules whose `root` nest (e.g. `"plugins"` and `"plugins/pc-core"`) are not a supported
configuration — the `<root>/<unit>/...` template reinterprets what "unit" means at each nesting
depth, so which rule "should" win has no well-defined answer independent of which fields each one
happens to set. Keep every rule's `root` disjoint from every other's.

Same envelope as `privacy_egress.py`: `permissionDecision: "deny"` in JSON stdout, process exit 0
always — never `exit 2`, that contract belongs to the PostToolUse `post_edit_lint.py` (IMP-0023-29).

Only `Write` is in scope: `Edit` cannot create a new file, so misplacement cannot occur through it.

Registered as a PreToolUse (Write) hook in hooks/hooks.json.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from policy_lib import load_policies, resolve_repo_root  # noqa: E402


def _decision(reason: str) -> dict:
    """Wrap a PreToolUse deny in the harness-expected envelope — same shape as privacy_egress."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _warn(message: str) -> None:
    print(f"structure_guard: {message}", file=sys.stderr)


def _as_list(value: object, where: str) -> list:
    """`value` if it is really a list, else `[]` with a warning — a rule author's typo (a bare
    string instead of a list) must never be silently reinterpreted as something else."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    _warn(f"`{where}` must be a list, got {type(value).__name__} — ignored.")
    return []


def _as_dict(value: object, where: str) -> dict:
    """`value` if it is really a dict, else `{}` with a warning."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    _warn(f"`{where}` must be an object, got {type(value).__name__} — ignored.")
    return {}


def _matching_rule(
    rules: list[dict], repo_root: Path, file_path: str
) -> tuple[dict, str, list[str]] | None:
    """The first rule, in declaration order, whose `root` is an ancestor of `file_path` AND
    yields a non-degenerate match, plus the unit name and the parts under it — None if no rule
    applies. A rule whose `root` matches but only as the bare unit segment (nothing under it) is
    skipped, not treated as a search-ending failure: a later, unrelated rule for the same event
    must still get its chance."""
    try:
        resolved = Path(file_path).resolve()
    except OSError:
        return None
    for rule in rules:
        root = rule.get("root")
        if not root:
            continue
        try:
            rel = resolved.relative_to((repo_root / root).resolve())
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) < 2:
            continue
        return rule, parts[0], list(parts[1:])
    return None


def decide(event: dict, repo_root: Path, rules: list[dict]) -> dict | None:
    """`rules` is the repo's own declared `file_placement` value — pure structural check, no I/O.

    Rule order matters: `forbidden_dirs` is checked before the unit-root shape because it is a
    stronger, unconditional veto with a more specific message; `reserved_dirs` and `leaf_subdirs`
    only apply once the directory is already known to be a recognised root_dir.
    """
    if event.get("tool_name") != "Write":
        return None
    file_path = (event.get("tool_input") or {}).get("file_path")
    if not file_path:
        return None
    matched = _matching_rule(rules, repo_root, file_path)
    if matched is None:
        return None
    rule, unit, rest = matched
    root = rule.get("root", "")

    forbidden_dirs = set(_as_list(rule.get("forbidden_dirs"), "forbidden_dirs"))
    hit = next((seg for seg in rest[:-1] if seg in forbidden_dirs), None)
    if hit is not None:
        return _decision(
            f"File placement: `{hit}/` is never a valid directory under `{root}/{unit}/` — "
            f"`{unit}/{'/'.join(rest)}` was rejected."
        )

    head = rest[0]
    root_dirs = set(_as_list(rule.get("root_dirs"), "root_dirs"))
    root_files = set(_as_list(rule.get("root_files"), "root_files"))
    if len(rest) == 1:
        if root_files and head not in root_files:
            belongs = f"one of {sorted(root_dirs)}" if root_dirs else "a subdirectory"
            return _decision(
                f"File placement: only {sorted(root_files)} are allowed directly at "
                f"`{root}/{unit}/` — `{unit}/{head}` was rejected. Anything else belongs under "
                f"{belongs}."
            )
        return None
    if root_dirs and head not in root_dirs:
        return _decision(
            f"File placement: `{unit}/{head}/` is not a recognised directory under "
            f"`{root}/{unit}/` — use one of {sorted(root_dirs)}."
        )

    reserved_dirs = _as_dict(rule.get("reserved_dirs"), "reserved_dirs")
    owner = reserved_dirs.get(head)
    if owner is not None and unit != owner:
        return _decision(
            f"File placement: `{head}/` under `{root}/{unit}/` is reserved for `{owner}` — "
            f"`{unit}/{head}/...` was rejected."
        )

    leaf_subdirs = _as_dict(rule.get("leaf_subdirs"), "leaf_subdirs")
    spec = _as_dict(leaf_subdirs.get(head), f"leaf_subdirs.{head}")
    if spec:
        required_file = spec.get("required_file")
        subdirs = set(_as_list(spec.get("subdirs"), f"leaf_subdirs.{head}.subdirs"))
        name = rest[1]
        remainder = rest[2:]
        if not remainder:
            # A flat file directly at `<unit>/<head>/<name>` — the container directory this
            # spec requires (e.g. a skill folder) was never created at all.
            must_be = f" containing `{required_file}`" if required_file else ""
            return _decision(
                f"File placement: `{unit}/{head}/{name}` must be a directory{must_be} — a flat "
                f"file there was rejected."
            )
        if len(remainder) == 1:
            if required_file and remainder[0] != required_file:
                return _decision(
                    f"File placement: a file directly under `{head}/{name}/` must be "
                    f"`{required_file}` — supporting files go in one of {sorted(subdirs)} — "
                    f"`{unit}/{head}/{name}/{remainder[0]}` was rejected."
                )
        elif remainder[0] not in subdirs:
            return _decision(
                f"File placement: `{head}/{name}/{remainder[0]}/` is not a recognised "
                f"supporting-file directory — use one of {sorted(subdirs)}."
            )

    return None


def run_hook() -> int:
    """Read the PreToolUse event from stdin, emit a deny when a rule applies, never raise."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0
    try:
        if event.get("tool_name") != "Write":
            return 0
        file_path = (event.get("tool_input") or {}).get("file_path")
        if not file_path:
            return 0
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
        repo_root = resolve_repo_root(project_dir, file_path)
        policies = load_policies(repo_root)
        rules = policies.get("file_placement") if policies else None
        if not isinstance(rules, list) or not rules:
            return 0
        decision = decide(event, repo_root, rules)
    except Exception:  # a guard must never block a tool call on its own bug
        return 0
    if decision is not None:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(run_hook())
