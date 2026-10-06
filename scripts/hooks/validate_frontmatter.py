#!/usr/bin/env python3
r"""pre-commit hook — validate YAML frontmatter for every artefact type this repo ships.

Type is detected by location, each with its own schema:
  - plugins/*/skills/*/SKILL.md  — skill frontmatter (claude_docs/skills.md)
  - plugins/*/agents/*.md        — agent frontmatter (claude_docs/sub-agents.md)

`commands/*.md` is the legacy pre-skills format this repo replaces — its presence anywhere is a
hard failure, not a missing-type gap to fill in later.

Skill schema, beyond the `name`/`description` presence floor:
  - `name` kebab-case (`^[a-z0-9]+(-[a-z0-9]+)*$`), equal to the skill directory name, ≤ 64 chars
    — the directory gives the slash-command name for a non-root skill, and the runtime loads a
    skill named `debug_brief` without complaint, so only the hook catches a malformed name.
  - `description` ≤ 1024 chars, no XML-style tags (`<url>`, `</agent>` — angle-bracket
    placeholders read as markup), and carrying a usage trigger (warn only: the "when", not just
    the "what").
  - body under 500 lines; every local `.md` link resolves (plain links only — a
    `file.md#fragment` link is not resolved, none exists in the corpus); a linked reference
    over 100 lines opens on `## Contents` / `## Sommaire` within its first 40 lines; every
    `.md` sitting DIRECTLY in the skill directory is mentioned from the SKILL.md (anti-orphan).
  - a body that takes arguments (`$ARGUMENTS`, `$1`, `arguments:`) declares `argument-hint`
    (warn only).

Deliberately NOT checked — measured false positives on the plugin corpus, do not re-add:
  - Windows-style `\` paths in bodies: shell quoting (`'\''`), literal `\t` in output templates
    and regexes like `s#\.git$##` false-positive for zero true positive.
  - A recursive "every `.md` under the skill dir must be linked" orphan scan: skills reference
    files by pattern (`references/type-<type>.md`) or read them from scripts at runtime. Only
    `.md` files directly in the skill directory are required to be mentioned.
  - Third-person heuristic on `description`: first-person trigger quotes ("I'm torn between")
    are legitimate, the pattern does not discriminate.

Usage:
    python3 validate_frontmatter.py [file ...]   # pre-commit passes staged .md paths
    python3 validate_frontmatter.py               # no args -> scan the whole repo
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


@dataclass
class Report:
    """Validation outcome — `problems` fail the hook, `warnings` only surface in output."""

    problems: list[str]
    warnings: list[str]


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
REQUIRED_COMMON = {"name", "description"}
BOOLEAN_FIELDS = {"disable-model-invocation", "user-invocable"}
KNOWN_CONTEXT_VALUES = {"fork"}

KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
XML_TAG_RE = re.compile(r"<[A-Za-z/][^>]*>")
TRIGGER_RE = re.compile(r"when|whenever|before|after|use for|trigger", re.IGNORECASE)
ARGS_RE = re.compile(r"\$ARGUMENTS|\$1\b|^\s*arguments:", re.MULTILINE)
MD_LINK_RE = re.compile(r"\]\(([^)#\s]+\.md)\)")
TOC_HEADING_RE = re.compile(r"^##\s+(?:Contents|Sommaire)\b", re.MULTILINE)

NAME_MAX_CHARS = 64
DESCRIPTION_MAX_CHARS = 1024
BODY_MAX_LINES = 500
REFERENCE_TOC_MIN_LINES = 100
REFERENCE_TOC_HEAD_LINES = 40


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


def _reference_problems(path: Path, body: str) -> list[str]:
    """Check the local `.md` links of a skill body: existence, TOC on long files, orphans."""
    problems = []
    for link in MD_LINK_RE.findall(body):
        if "://" in link or link.startswith("/"):
            continue
        target = (path.parent / link).resolve()
        if not target.exists():
            problems.append(f"{path}: linked reference `{link}` not found")
            continue
        try:
            lines = target.read_text().splitlines()
        except UnicodeDecodeError:
            problems.append(
                f"{target}: not valid UTF-8 — cannot check for a table of contents"
            )
            continue
        if len(lines) > REFERENCE_TOC_MIN_LINES and not TOC_HEADING_RE.search(
            "\n".join(lines[:REFERENCE_TOC_HEAD_LINES])
        ):
            problems.append(
                f"{target}: {len(lines)} lines but no `## Contents` / `## Sommaire` "
                f"within the first {REFERENCE_TOC_HEAD_LINES} lines"
            )
    for md in sorted(path.parent.glob("*.md")):
        if md.name != "SKILL.md" and md.name not in body:
            problems.append(
                f"{md}: not mentioned from SKILL.md — every .md in the skill "
                f"directory must be reachable from the skill"
            )
    return problems


def validate_skill(path: Path, fields: dict[str, str], body: str) -> Report:
    """Check skill-specific rules: frontmatter shape, reference hygiene.

    Warnings are printed but never fail the hook.
    """
    problems: list[str] = []
    warnings: list[str] = []
    name = fields.get("name", "")
    description = fields.get("description", "")

    if name and not KEBAB_RE.match(name):
        problems.append(
            f"{path}: `name: {name}` must be kebab-case `^[a-z0-9]+(-[a-z0-9]+)*$`"
        )
    if not KEBAB_RE.match(path.parent.name):
        problems.append(
            f"{path}: skill directory `{path.parent.name}` must be kebab-case "
            f"`^[a-z0-9]+(-[a-z0-9]+)*$`"
        )
    if name and name != path.parent.name:
        problems.append(
            f"{path}: `name: {name}` must equal the skill directory name `{path.parent.name}`"
        )
    if len(name) > NAME_MAX_CHARS:
        problems.append(f"{path}: `name` is {len(name)} chars, max {NAME_MAX_CHARS}")
    if len(description) > DESCRIPTION_MAX_CHARS:
        problems.append(
            f"{path}: `description` is {len(description)} chars, "
            f"max {DESCRIPTION_MAX_CHARS}"
        )
    xml_tag = XML_TAG_RE.search(description)
    if xml_tag:
        problems.append(
            f"{path}: `description` carries an XML-style tag (`{xml_tag.group(0)}`) — "
            f"keep the description plain text, angle-bracket placeholders read as markup"
        )

    context = fields.get("context")
    if context and context not in KNOWN_CONTEXT_VALUES:
        problems.append(f'{path}: `context: {context}` — only "fork" is a known value')
    if context == "fork" and not fields.get("agent"):
        problems.append(f"{path}: `context: fork` set but no `agent:` declared")
    for field in BOOLEAN_FIELDS:
        value = fields.get(field)
        if value is not None and value not in ("true", "false"):
            problems.append(f"{path}: `{field}: {value}` must be `true` or `false`")

    if len(body.splitlines()) >= BODY_MAX_LINES:
        problems.append(
            f"{path}: body is {len(body.splitlines())} lines, "
            f"must stay under {BODY_MAX_LINES}"
        )
    problems += _reference_problems(path, body)

    if ARGS_RE.search(body) and not fields.get("argument-hint"):
        warnings.append(
            f"{path}: body takes arguments (`$ARGUMENTS` / `$1` / `arguments:`) "
            f"but no `argument-hint` in frontmatter"
        )
    if description and not TRIGGER_RE.search(description):
        warnings.append(
            f"{path}: `description` says what the skill does but not when to use it "
            f"(add a trigger: when / before / after / Use for / TRIGGER)"
        )
    return Report(problems, warnings)


def validate_agent(path: Path, fields: dict[str, str], body: str) -> Report:
    """Check agent-specific frontmatter rules.

    No agent-specific constraints verified yet beyond the common name/description floor — add
    checks here once a real agent ships and a real schema requirement is confirmed.
    """
    return Report([], [])


def validate_file(path: Path, kind: str) -> Report:
    """Validate one artefact file against its type's schema (`kind`: skill/agent).

    Problems fail the hook, warnings do not.
    """
    text = path.read_text()
    fields = parse_frontmatter(text)
    if fields is None:
        return Report([f"{path}: no YAML frontmatter block (--- ... ---) found"], [])

    problems = [
        f"{path}: missing or empty required field `{field}`"
        for field in REQUIRED_COMMON
        if not fields.get(field)
    ]
    body = text[FRONTMATTER_RE.match(text).end() :]  # type: ignore[union-attr]
    report = (
        validate_skill(path, fields, body)
        if kind == "skill"
        else validate_agent(path, fields, body)
    )
    return Report(problems + report.problems, report.warnings)


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
    all_warnings: list[str] = []
    for path in skill_files:
        report = validate_file(path, "skill")
        all_problems.extend(report.problems)
        all_warnings.extend(report.warnings)
    for path in agent_files:
        report = validate_file(path, "agent")
        all_problems.extend(report.problems)
        all_warnings.extend(report.warnings)

    for warning in all_warnings:
        print(f"WARN {warning}")
    if all_problems:
        for problem in all_problems:
            print(problem)
        return 1

    total = len(skill_files) + len(agent_files)
    print(f"{total} artefact(s) OK (0 legacy commands/*.md found).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
