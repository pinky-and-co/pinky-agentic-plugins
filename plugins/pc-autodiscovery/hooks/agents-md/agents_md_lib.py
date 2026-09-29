"""Rendering library for the workspace, group and repo AGENTS.md files.

Every section below the title of a workspace or group AGENTS.md is generator-owned and replaced
wholesale: only the title line and the free-text intro before the first `##` heading survive, so a
hand-written `##` section in those files is dropped on the next render. The generated sections
are wrapped in marker comments so a reader can tell them from hand-written text.

The workspace profile lives in `<workspace_root>/.claude/settings.pinky.json`: its `behavior` and
`gotchas` keys (a mapping or a list of strings) feed the matching sections. A missing or invalid
profile means empty sections, never an exception. Nothing here creates a symlink or touches a
CLAUDE.md.

Text that ends up in a generated file (pyproject descriptions, profile values) is sanitised: HTML
comments are stripped, so a value can never forge or close a marker block.

Stays importable on Python 3.9 (the hook may run under an older `python3`): `tomllib` is imported
lazily and its absence only blanks descriptions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

MARK = {
    "packages": (
        "<!-- pinky:agents-packages:start -->",
        "<!-- pinky:agents-packages:end -->",
    ),
    "cross_package_patterns": (
        "<!-- pinky:agents-cross-package-patterns:start -->",
        "<!-- pinky:agents-cross-package-patterns:end -->",
    ),
    "gotchas": (
        "<!-- pinky:agents-gotchas:start -->",
        "<!-- pinky:agents-gotchas:end -->",
    ),
    "behavior": (
        "<!-- pinky:agents-behavior:start -->",
        "<!-- pinky:agents-behavior:end -->",
    ),
}

ACCUMULATION_NOTE = (
    "AGENTS.md accumulates: root → repo, each level states only its own layer."
)

WORKSPACE_HEADINGS = [
    "## Packages",
    "## Cross-Package Patterns",
    "## Gotchas",
    "## Behavior policy",
]
GROUP_HEADINGS = ["## Packages", "## Cross-Package Patterns"]

DESCRIPTION_LIMIT = 300

# Dossiers de dépendances : jamais des groupes ni des repos du workspace
_SKIP_DIRS = frozenset({"node_modules", "vendor", ".venv", "venv", "site-packages"})

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_DELIMITER_RE = re.compile(r"<!--|-->")
_BLOCK_RE = re.compile(
    r"^<!-- pinky:agents-(?P<k>[a-z-]+):start -->.*?^<!-- pinky:agents-(?P=k):end -->",
    re.S | re.M,
)
_MARKER_PREFIX = "<!-- pinky:agents-"


def is_valid_workspace_root(path: Path) -> bool:
    """Whether `path` may serve as a workspace root.

    A directory holding a `.git` entry (file or directory) is a repo, and `$HOME` is never a
    workspace: both are refused.

    Args:
        path: Candidate directory.

    Returns:
        True when `path` is an existing directory that is neither a repo nor `$HOME`.
    """
    if not path.is_dir() or os.path.lexists(path / ".git"):
        return False
    try:
        home: Path | None = Path.home().resolve()
    except (RuntimeError, OSError):
        home = None
    try:
        return path.resolve() != home
    except OSError:
        return False


def find_workspace_root(start: Path, environ: Mapping[str, str]) -> Path | None:
    """Locate the workspace root.

    `PINKY_WORKSPACE_ROOT` (when set to an existing directory) wins; otherwise the nearest
    directory at or above `start` holding a `*.code-workspace` file. A resolved root that fails
    `is_valid_workspace_root` (a repo shipping its own workspace file, `$HOME`) yields None.
    No fallback: outside any workspace the answer is None.

    Args:
        start: Directory to start the upward walk from.
        environ: Environment mapping to read `PINKY_WORKSPACE_ROOT` from.

    Returns:
        The workspace root, or None when none can be resolved.
    """
    explicit = environ.get("PINKY_WORKSPACE_ROOT")
    if explicit and Path(explicit).is_dir():
        root = Path(explicit)
        return root if is_valid_workspace_root(root) else None
    try:
        current = start.resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        try:
            has_marker = any(candidate.glob("*.code-workspace"))
        except OSError:
            continue
        if has_marker:
            return candidate if is_valid_workspace_root(candidate) else None
    return None


def atomic_write(path: Path, content: str) -> None:
    """Write `content` to `path` through a temp file and `os.replace`.

    The temp file is fsynced before the rename, takes the mode of an existing target (or the
    umask default for a new file), and is removed on any failure.

    Args:
        path: Target file.
        content: Text to write.

    Raises:
        BaseException: Whatever the write raised, after the temp file is removed.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".agents-md-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copymode(str(path), tmp_name)
        else:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp_name, 0o666 & ~umask)
        os.replace(tmp_name, str(path))
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _strip_comments(text: str) -> str:
    """Remove every HTML comment, then any dangling `<!--` / `-->`, until nothing is left."""
    for pattern in (_COMMENT_RE, _DELIMITER_RE):
        previous: str | None = None
        while previous != text:
            previous = text
            text = pattern.sub("", text)
    return text


def _discover_repos_under(workspace_root: Path) -> list[Path]:
    """Every git repo under `workspace_root`, up to one directory deep.

    Symlinked directories are not followed and dependency directories are skipped. Repos are keyed
    by their path relative to the workspace, so two same-named repos in different groups both
    appear.
    """
    seen: dict[Path, Path] = {}
    for path in sorted(workspace_root.iterdir()):
        if path.name.startswith(".") or path.name in _SKIP_DIRS:
            continue
        if path.is_symlink() or not path.is_dir():
            continue
        if (path / ".git").exists():
            seen[path.relative_to(workspace_root)] = path
            continue
        try:
            nested_dirs = sorted(path.iterdir())
        except OSError:
            continue
        for nested in nested_dirs:
            if nested.name.startswith(".") or nested.name in _SKIP_DIRS:
                continue
            if nested.is_symlink() or not nested.is_dir():
                continue
            if (nested / ".git").exists():
                seen[nested.relative_to(workspace_root)] = nested
    return list(seen.values())


def tomllib_available() -> bool:
    """Whether `tomllib` (Python 3.11+) can be imported.

    Returns:
        False on an older interpreter, where descriptions cannot be read.
    """
    try:
        import tomllib  # noqa: F401
    except ImportError:
        return False
    return True


def _pyproject_description(repo: Path) -> str:
    """`[project].description` of `repo/pyproject.toml`, or "" if absent or unparseable."""
    try:
        import tomllib

        data = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError):
        return ""
    project = data.get("project", {})
    description = project.get("description", "") if isinstance(project, dict) else ""
    return description if isinstance(description, str) else ""


def _table_cell(text: str) -> str:
    """Make `text` safe as one Markdown table cell inside a generated block."""
    text = " ".join(_strip_comments(text).split())
    if len(text) > DESCRIPTION_LIMIT:
        text = text[: DESCRIPTION_LIMIT - 1].rstrip() + "…"
    return text.replace("|", "\\|")


def _description_cell(repo: Path) -> str:
    return _table_cell(_pyproject_description(repo)) or "—"


def packages_table(workspace_root: Path) -> str:
    """One row per top-level directory that contains a repo (itself, or nested one level deep).

    Args:
        workspace_root: The workspace directory.

    Returns:
        The Markdown table.
    """
    by_top: dict[str, list[Path]] = {}
    for repo in _discover_repos_under(workspace_root):
        top = repo.relative_to(workspace_root).parts[0]
        by_top.setdefault(top, []).append(repo)

    rows = ["| Package | Description | Path |", "|---|---|---|"]
    for top in sorted(by_top):
        repos = by_top[top]
        if len(repos) == 1 and repos[0].name == top:
            desc = _description_cell(repos[0])
        else:
            desc = _table_cell(", ".join(sorted(r.name for r in repos)))
        rows.append(f"| {_table_cell(top)} | {desc} | `{_table_cell(top)}/` |")
    return "\n".join(rows)


def discover_groups(workspace_root: Path) -> list[Path]:
    """Every top-level directory holding one or more repos nested one level down.

    Args:
        workspace_root: The workspace directory.

    Returns:
        The group directories, sorted by name.
    """
    by_top: dict[str, list[Path]] = {}
    for repo in _discover_repos_under(workspace_root):
        top = repo.relative_to(workspace_root).parts[0]
        by_top.setdefault(top, []).append(repo)
    return [
        workspace_root / top
        for top, repos in sorted(by_top.items())
        if not (len(repos) == 1 and repos[0].name == top)
    ]


def _profile_bullets(workspace_root: Path, key: str) -> str:
    """Render `settings.pinky.json[key]` (mapping or list of strings) as `- text` lines."""
    settings_path = workspace_root / ".claude" / "settings.pinky.json"
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    value = data.get(key)
    if isinstance(value, dict):
        items = list(value.values())
    elif isinstance(value, list):
        items = value
    else:
        return ""
    cleaned = [
        " ".join(_strip_comments(t).split()) for t in items if isinstance(t, str)
    ]
    return "\n".join(f"- {text}" for text in cleaned if text)


def gotchas_section(workspace_root: Path) -> str:
    """Render `settings.pinky.json["gotchas"]` in file order; empty when absent.

    Args:
        workspace_root: The workspace directory.

    Returns:
        One `- text` line per gotcha, or an empty string.
    """
    return _profile_bullets(workspace_root, "gotchas")


def behavior_section(workspace_root: Path) -> str:
    """Render `settings.pinky.json["behavior"]` in file order; empty when absent.

    Args:
        workspace_root: The workspace directory.

    Returns:
        One `- text` line per behavior rule, or an empty string.
    """
    return _profile_bullets(workspace_root, "behavior")


def _wrapped(key: str, body: str) -> str:
    start, end = MARK[key]
    return f"{start}\n\n{body}\n\n{end}"


def _intro_end(text: str) -> int | None:
    """Offset where the free intro of `text` stops, or None when all of it is intro.

    The intro ends at the first `## ` heading line or the first line-initial generator marker, whichever
    comes first. Plain text search: no Markdown parsing, so a stray code fence cannot change the answer.
    """
    cuts = []
    if text.startswith("## "):
        cuts.append(0)
    heading = text.find("\n## ")
    if heading != -1:
        cuts.append(heading)
    if text.startswith(_MARKER_PREFIX):
        cuts.append(0)
    marker = text.find("\n" + _MARKER_PREFIX)
    if marker != -1:
        cuts.append(marker)
    return min(cuts) if cuts else None


def _intro(default: str, existing: str | None) -> str:
    """The title and free intro of `existing` (text before the first heading or marker).

    A file without any heading keeps its whole text as intro; one whose text before the first
    heading is blank gets `default`.
    """
    if not existing or not existing.strip():
        return default
    end = _intro_end(existing)
    if end is None:
        return existing.rstrip("\n") + "\n"
    head = existing[:end]
    if not head.strip():
        return default
    return head.rstrip("\n") + "\n"


def has_foreign_sections(existing: str | None, allowed: Sequence[str]) -> bool:
    """Whether a render would drop hand-written content from `existing`.

    Everything after the intro, once the marker-wrapped blocks and the generator's own headings
    are taken out, is hand-written and would be replaced.

    Args:
        existing: Current file content, if any.
        allowed: The headings the generator owns at this level.

    Returns:
        True when a render would drop hand-written content.
    """
    if not existing:
        return False
    end = _intro_end(existing)
    if end is None:
        return False
    rest = _BLOCK_RE.sub("", existing[end:])
    return any(
        line.strip() and line.strip() not in allowed for line in rest.splitlines()
    )


def render_workspace_agents_md(workspace_root: Path, existing: str | None) -> str:
    """Build the full workspace AGENTS.md content.

    Args:
        workspace_root: The workspace directory.
        existing: Current file content, if any; only its title and intro survive.

    Returns:
        The full file content.
    """
    intro = _intro(f"# AGENTS.md — {workspace_root.name}\n", existing)
    sections = [
        intro.rstrip("\n"),
        "## Packages\n\n" + _wrapped("packages", packages_table(workspace_root)),
        "## Cross-Package Patterns\n\n"
        + _wrapped("cross_package_patterns", ACCUMULATION_NOTE),
        "## Gotchas\n\n" + _wrapped("gotchas", gotchas_section(workspace_root)),
        "## Behavior policy\n\n"
        + _wrapped("behavior", behavior_section(workspace_root)),
    ]
    return "\n\n".join(sections) + "\n"


def render_group_agents_md(
    group_root: Path, workspace_root: Path, existing: str | None
) -> str:
    """Build a group AGENTS.md: Packages + Cross-Package Patterns for its nested repos only.

    Args:
        group_root: The group directory.
        workspace_root: The workspace directory.
        existing: Current file content, if any; only its title and intro survive.

    Returns:
        The full file content.
    """
    name = group_root.name
    intro = _intro(f"# AGENTS.md — {name}\n", existing)
    repos = [
        r
        for r in _discover_repos_under(workspace_root)
        if r.relative_to(workspace_root).parts[0] == name
    ]
    rows = ["| Package | Description | Path |", "|---|---|---|"]
    for r in sorted(repos, key=lambda p: p.name):
        name_cell = _table_cell(r.name)
        rows.append(f"| {name_cell} | {_description_cell(r)} | `{name_cell}/` |")

    sections = [
        intro.rstrip("\n"),
        "## Packages\n\n" + _wrapped("packages", "\n".join(rows)),
        "## Cross-Package Patterns\n\n"
        + _wrapped("cross_package_patterns", ACCUMULATION_NOTE),
    ]
    return "\n\n".join(sections) + "\n"


def _context_md_glossary(repo: Path) -> str:
    """The naming-glossary body of `<repo>/CONTEXT.md`: its text minus a leading `# ` title line."""
    path = repo / "CONTEXT.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("# "):
        text = text.split("\n", 1)[1].strip() if "\n" in text else ""
    return text


def _repo_default_body(repo: Path) -> str:
    """The hand-authored body a never-seeded repo AGENTS.md gets, folding in CONTEXT.md."""
    glossary = _context_md_glossary(repo)
    return (
        "## Naming glossary\n\n"
        f"{glossary or '_Not written yet._'}\n\n"
        "## Repo conventions\n\n"
        "_Not written yet._"
    )


def render_standalone_agents_md(
    repo: Path, workspace_root: Path, existing: str | None
) -> str:
    """Full render for a repo leaving the workspace: Gotchas + Behavior policy + its own body.

    Idempotent across runs: a prior standalone render is detected by the last Behavior-policy end
    marker and the body is re-extracted from after it, so generated sections never get mistaken
    for hand-authored content.

    Args:
        repo: The repo directory.
        workspace_root: The workspace whose profile feeds Gotchas and Behavior.
        existing: Current file content, if any.

    Returns:
        The full file content.
    """
    default_intro = f"# AGENTS.md — {repo.name}\n"
    intro = default_intro
    body: str | None = None

    if existing and existing.strip():
        intro = _intro(default_intro, existing)
        behavior_end = MARK["behavior"][1]
        end_at = existing.rfind(behavior_end)
        if end_at != -1:
            body = existing[end_at + len(behavior_end) :].strip()
        else:
            end = _intro_end(existing)
            if end is not None:
                body = existing[end:].strip()
    if body is None:
        body = _repo_default_body(repo)

    sections = [
        intro.rstrip("\n"),
        "## Gotchas\n\n" + _wrapped("gotchas", gotchas_section(workspace_root)),
        "## Behavior policy\n\n"
        + _wrapped("behavior", behavior_section(workspace_root)),
        body,
    ]
    return "\n\n".join(sections) + "\n"
