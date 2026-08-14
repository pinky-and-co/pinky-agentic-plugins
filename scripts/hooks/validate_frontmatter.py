#!/usr/bin/env python3
"""pre-commit hook — validate YAML frontmatter for every artefact type this repo ships.

Type is detected by location, each with its own schema:
  - plugins/*/skills/*/SKILL.md  — skill frontmatter (claude_docs/skills.md)
  - plugins/*/agents/*.md        — agent frontmatter (claude_docs/sub-agents.md)

`commands/*.md` is the legacy pre-skills format this repo replaces — its presence anywhere is a
hard failure, not a missing-type gap to fill in later.

Usage:
    python3 validate_frontmatter.py [file ...]   # pre-commit passes staged .md paths
    python3 validate_frontmatter.py               # no args -> scan the whole repo
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
REQUIRED_COMMON = {"name", "description"}
BOOLEAN_FIELDS = {"disable-model-invocation", "user-invocable"}
KNOWN_CONTEXT_VALUES = {"fork"}


def find_legacy_commands_dirs() -> list[Path]:
    """Return every `plugins/*/commands` directory found (the forbidden legacy format)."""
    return sorted(REPO_ROOT.glob("plugins/*/commands"))


def find_skill_files() -> list[Path]:
    """Return every `SKILL.md` under `plugins/*/skills/*/`."""
    return sorted(REPO_ROOT.glob("plugins/*/skills/*/SKILL.md"))


def find_agent_files() -> list[Path]:
    """Return every agent `.md` file under `plugins/*/agents/`."""
    return sorted(REPO_ROOT.glob("plugins/*/agents/*.md"))


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """Parse a leading `--- ... ---` YAML block into a flat key/value dict, or None if absent."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    fields: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def validate_skill(path: Path, fields: dict[str, str]) -> list[str]:
    """Check skill-specific frontmatter rules: `context`/`agent` pairing, boolean fields."""
    problems = []
    context = fields.get("context")
    if context and context not in KNOWN_CONTEXT_VALUES:
        problems.append(f'{path}: `context: {context}` — only "fork" is a known value')
    if context == "fork" and not fields.get("agent"):
        problems.append(f"{path}: `context: fork` set but no `agent:` declared")
    for field in BOOLEAN_FIELDS:
        value = fields.get(field)
        if value is not None and value not in ("true", "false"):
            problems.append(f"{path}: `{field}: {value}` must be `true` or `false`")
    return problems


def validate_agent(path: Path, fields: dict[str, str]) -> list[str]:
    """Check agent-specific frontmatter rules.

    No agent-specific constraints verified yet beyond the common name/description floor — add
    checks here once a real agent ships and a real schema requirement is confirmed.
    """
    return []


def validate_file(path: Path, kind: str) -> list[str]:
    """Validate one artefact file's frontmatter against its type's schema (`kind`: skill/agent)."""
    text = path.read_text()
    fields = parse_frontmatter(text)
    if fields is None:
        return [f"{path}: no YAML frontmatter block (--- ... ---) found"]

    problems = [
        f"{path}: missing or empty required field `{field}`"
        for field in REQUIRED_COMMON
        if not fields.get(field)
    ]
    problems += (
        validate_skill(path, fields)
        if kind == "skill"
        else validate_agent(path, fields)
    )
    return problems


def main() -> int:
    """Validate staged/given files, or scan the whole repo if none were passed."""
    args = [Path(p) for p in sys.argv[1:]]

    legacy_dirs = find_legacy_commands_dirs()
    if legacy_dirs:
        for d in legacy_dirs:
            print(
                f"{d}: commands/*.md is the legacy format this repo replaces — forbidden here"
            )
        return 1

    if args:
        skill_files = [p for p in args if p.match("*/skills/*/SKILL.md")]
        agent_files = [p for p in args if p.match("*/agents/*.md")]
    else:
        skill_files = find_skill_files()
        agent_files = find_agent_files()

    all_problems: list[str] = []
    for path in skill_files:
        all_problems.extend(validate_file(path, "skill"))
    for path in agent_files:
        all_problems.extend(validate_file(path, "agent"))

    if all_problems:
        for problem in all_problems:
            print(problem)
        return 1

    total = len(skill_files) + len(agent_files)
    print(f"{total} artefact(s) OK (0 legacy commands/*.md found).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
