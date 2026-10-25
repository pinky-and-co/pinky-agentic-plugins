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
    placeholders read as markup), and carrying a usage trigger (the "when", not just the "what").
  - body under 500 lines; every local `.md` link resolves (plain links only — a
    `file.md#fragment` link is not resolved, none exists in the corpus); a linked reference
    over 100 lines opens on `## Contents` / `## Sommaire` within its first 40 lines; every
    `.md` sitting DIRECTLY in the skill directory is mentioned from the SKILL.md (anti-orphan).
  - a body that takes arguments (`$ARGUMENTS`, `$1`, `arguments:`) declares `argument-hint`.

Every finding blocks (exit 1) and is printed through `_report` — a passing run prints nothing.

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
from pathlib import Path

from _report import Diagnostic, display, emit

REPO_ROOT = Path(__file__).parent.parent.parent
CODE = "FRONTMATTER"

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


def _finding(
    path: Path,
    text: str,
    message: str,
    *,
    needle: str | None = None,
    key: str | None = None,
    help: str | None = None,
) -> Diagnostic:
    """A finding on `path`, pointing at `needle` (first occurrence) or at the value of frontmatter
    field `key` when found in `text`, on the whole file otherwise."""
    where = display(path)
    for number, line in enumerate(text.splitlines(), start=1):
        if key is not None and line.partition(":")[0].strip() == key:
            value = line.partition(":")[2].strip()
            col = line.index(value, len(key)) + 1 if value else len(line) + 1
            span = needle or value
            if needle and needle in line:
                col = line.index(needle) + 1
            return Diagnostic(
                CODE, message, where, number, col, max(1, len(span)), help
            )
        if key is None and needle is not None and needle in line:
            return Diagnostic(
                CODE, message, where, number, line.index(needle) + 1, len(needle), help
            )
    return Diagnostic(CODE, message, where, help=help)


def _reference_problems(path: Path, body: str, text: str) -> list[Diagnostic]:
    """Check the local `.md` links of a skill body: existence, TOC on long files, orphans."""
    problems = []
    for link in MD_LINK_RE.findall(body):
        if "://" in link or link.startswith("/"):
            continue
        target = (path.parent / link).resolve()
        if not target.exists():
            problems.append(
                _finding(
                    path,
                    text,
                    f"linked reference `{link}` not found",
                    needle=link,
                )
            )
            continue
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            problems.append(
                _finding(
                    target,
                    "",
                    "not valid UTF-8 — cannot check for a table of contents",
                )
            )
            continue
        if len(lines) > REFERENCE_TOC_MIN_LINES and not TOC_HEADING_RE.search(
            "\n".join(lines[:REFERENCE_TOC_HEAD_LINES])
        ):
            problems.append(
                _finding(
                    target,
                    "",
                    f"{len(lines)} lines but no `## Contents` / `## Sommaire` "
                    f"within the first {REFERENCE_TOC_HEAD_LINES} lines",
                    help="open the reference with a table of contents",
                )
            )
    for md in sorted(path.parent.glob("*.md")):
        if md.name != "SKILL.md" and md.name not in body:
            problems.append(
                _finding(
                    md,
                    "",
                    "not mentioned from SKILL.md",
                    help="every .md in the skill directory must be reachable from the skill",
                )
            )
    return problems


def validate_skill(
    path: Path, fields: dict[str, str], body: str, text: str
) -> list[Diagnostic]:
    """Check skill-specific rules: frontmatter shape, reference hygiene."""
    problems: list[Diagnostic] = []
    name = fields.get("name", "")
    description = fields.get("description", "")
    kebab_help = (
        "use lowercase letters, digits and single hyphens: `^[a-z0-9]+(-[a-z0-9]+)*$`"
    )

    if name and not KEBAB_RE.match(name):
        problems.append(
            _finding(
                path, text, "`name` must be kebab-case", key="name", help=kebab_help
            )
        )
    if not KEBAB_RE.match(path.parent.name):
        problems.append(
            _finding(
                path,
                text,
                f"skill directory `{path.parent.name}` must be kebab-case",
                help=kebab_help,
            )
        )
    if name and name != path.parent.name:
        problems.append(
            _finding(
                path,
                text,
                f"`name` must equal the skill directory name `{path.parent.name}`",
                key="name",
            )
        )
    if len(name) > NAME_MAX_CHARS:
        problems.append(
            _finding(
                path,
                text,
                f"`name` is {len(name)} chars, max {NAME_MAX_CHARS}",
                key="name",
            )
        )
    if len(description) > DESCRIPTION_MAX_CHARS:
        problems.append(
            _finding(
                path,
                text,
                f"`description` is {len(description)} chars, max {DESCRIPTION_MAX_CHARS}",
                key="description",
            )
        )
    xml_tag = XML_TAG_RE.search(description)
    if xml_tag:
        problems.append(
            _finding(
                path,
                text,
                "`description` carries an XML-style tag",
                key="description",
                needle=xml_tag.group(0),
                help="keep the description plain text — angle-bracket placeholders read as markup",
            )
        )

    context = fields.get("context")
    if context and context not in KNOWN_CONTEXT_VALUES:
        problems.append(
            _finding(
                path,
                text,
                f'`context: {context}` — only "fork" is a known value',
                key="context",
            )
        )
    if context == "fork" and not fields.get("agent"):
        problems.append(
            _finding(
                path,
                text,
                "`context: fork` set but no `agent:` declared",
                key="context",
            )
        )
    for field in sorted(BOOLEAN_FIELDS):
        value = fields.get(field)
        if value is not None and value not in ("true", "false"):
            problems.append(
                _finding(
                    path,
                    text,
                    f"`{field}: {value}` must be `true` or `false`",
                    key=field,
                )
            )

    if len(body.splitlines()) >= BODY_MAX_LINES:
        problems.append(
            _finding(
                path,
                text,
                f"body is {len(body.splitlines())} lines, must stay under {BODY_MAX_LINES}",
                help="move detail into a linked reference file",
            )
        )
    problems += _reference_problems(path, body, text)

    args = ARGS_RE.search(body)
    if args and not fields.get("argument-hint"):
        problems.append(
            _finding(
                path,
                text,
                "body takes arguments but declares no `argument-hint`",
                needle=args.group(0).strip(),
                help='add `argument-hint: "[...]"` to the frontmatter',
            )
        )
    if description and not TRIGGER_RE.search(description):
        problems.append(
            _finding(
                path,
                text,
                "`description` says what the skill does but not when to use it",
                key="description",
                help="add a trigger: when / before / after / Use for / TRIGGER",
            )
        )
    return problems


def validate_agent(path: Path, fields: dict[str, str], body: str) -> list[Diagnostic]:
    """Check agent-specific frontmatter rules.

    No agent-specific constraints verified yet beyond the common name/description floor — add
    checks here once a real agent ships and a real schema requirement is confirmed.
    """
    return []


def validate_file(path: Path, kind: str) -> list[Diagnostic]:
    """Validate one artefact file against its type's schema (`kind`: skill/agent)."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [
            _finding(
                path,
                "",
                "not valid UTF-8 — cannot check the frontmatter",
            )
        ]
    fields = parse_frontmatter(text)
    if fields is None:
        return [
            _finding(
                path,
                text,
                "no YAML frontmatter block found",
                help="open the file with `---` ... `---`",
            )
        ]

    problems = [
        _finding(
            path,
            text,
            f"missing or empty required field `{field}`",
            key=field if field in fields else None,
            help=f"declare `{field}:` in the frontmatter",
        )
        for field in sorted(REQUIRED_COMMON)
        if not fields.get(field)
    ]
    body = text[FRONTMATTER_RE.match(text).end() :]  # type: ignore[union-attr]
    if kind == "skill":
        return problems + validate_skill(path, fields, body, text)
    return problems + validate_agent(path, fields, body)


def main() -> int:
    """Validate staged/given files, or scan the whole repo if none were passed."""
    args = [Path(p) for p in sys.argv[1:]]

    findings = [
        Diagnostic(
            CODE,
            "commands/*.md is the legacy format this repo replaces — forbidden here",
            display(d),
            help="migrate each command to `skills/<name>/SKILL.md`",
        )
        for d in find_legacy_commands_dirs()
    ]

    if args:
        skill_files = [p for p in args if p.match("*/skills/*/SKILL.md")]
        agent_files = [p for p in args if p.match("*/agents/*.md")]
    else:
        skill_files = find_skill_files()
        agent_files = find_agent_files()

    if not findings:
        for path in skill_files:
            findings += validate_file(path, "skill")
        for path in agent_files:
            findings += validate_file(path, "agent")

    emit(findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
