#!/usr/bin/env python3
"""resolve_policy — deterministic lookup into a repo's .claude/settings.pinky.json, for DCI
(`` !`command` `` in a skill body) instead of asking the model to read and interpret CLAUDE.md
prose. Reuses policy_lib.py's load_policies()/resolve_repo_root() — one resolution path for both
the PreToolUse hooks (which cannot reach a forked skill, see architecture findings §hook-fork-
test) and this script (which DCI pre-renders before any fork sees the prompt, so it does).

Usage:
    python3 resolve_policy.py <key> [--repo <path>]

Prints the raw value for <key> (e.g. "git flow" for branch_workflow), or nothing if the key or
settings.pinky.json is absent — never an error, so a missing declaration degrades to an empty DCI
substitution rather than a broken skill render.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from policy_lib import load_policies, resolve_repo_root  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return 0

    key = args[0]
    repo_arg = None
    if "--repo" in args:
        repo_arg = args[args.index("--repo") + 1]

    repo_root = resolve_repo_root(repo_arg or ".", None)
    policies = load_policies(repo_root)
    if policies is None:
        return 0

    value = policies.get(key)
    if value is not None:
        print(value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
