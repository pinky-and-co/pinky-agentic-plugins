#!/usr/bin/env python3
"""Inventory of the memory notes of the current level and of its parent levels, as JSON.

Usage: `inventory.py [--cwd PATH]`. Deterministic, no model, and it never writes anything: not a
ledger, not a marker, not a corrupt ledger's rename.

Levels are the ones the SessionStart hook shows (`pc_memory_lib.collect_levels`): the current
level and each ancestor level that has a `MEMORY.md`, nearest first. A directory declared only in
the shared project settings, which a cloned repo controls, is not listed: `apply.py` would refuse
it. Per level:

    {"level": "~/ws/repo", "dir": "~/.claude/projects/-ws-repo/memory", "role": "current",
     "last_review": null | "YYYY-MM-DD",
     "index": {"lines": N, "bytes": N, "over_budget": bool},
     "notes": [{"file", "description", "type", "age_days", "sessions", "last_read",
                "in_index", "index_line", "coverage"}],
     "orphans": {"index_without_file": [...], "file_without_index": [...]}}

`duplicates.exact` lists the groups of notes, across every level shown, whose body after the
frontmatter is the same once whitespace is normalised. A note with an empty body is not compared.

`index_line` is the first index line linking the note, null when none does. Every text read from a note, an index or the
ledger (descriptions, types, index lines, file names, orphan and duplicate names) has its control
characters removed and is capped, so it is safe to show to a model; `last_read` and `last_review`
are kept only when they are `YYYY-MM-DD` dates, else null.
Dot files (ledger, lock, `.corrupt`, temp files, the trash folder) and `MEMORY.md` are not notes.

`coverage` is a hint that a note's fact is already stated in an instruction source, so the note can
be deleted: up to 3 `{"file": "~/...", "line": N, "text": "<line, 160 chars at most>"}`, best
first, `terms` being the words that matched. The sources are read from the root directory of every level shown: `CLAUDE.md`,
`AGENTS.md`, `.claude/rules/*.md`, the string values of `.claude/settings.pinky.json`, every
`skills/<name>/SKILL.md` below the root and the `description` fields of every `hooks/hooks.json`.
Terms are the words of 6+ characters of the note's name and description and its backticked
identifiers, minus common words and a list of the repo's own vocabulary (plugin, hooks, skill,
scripts, working, tree, files, repo, code, claude, session, memory, note, rule). A term found in
more than 10% of the lines or of the files read, with a floor of 3, is common and weighs a tenth of
a rare one. A source covers a note when a window of 3 lines holds at least 4 distinct rare terms,
or one rare backticked identifier of 8+ characters. The hint is a lead, not a verdict: read the
cited line. Bounded:
at most 60 source files per level, none over 200 KB, nothing below 6 directories. A file is read
through its real path, once, whatever the number of names that lead to it (`CLAUDE.md` is often a
link to `AGENTS.md`): the first name met is cited, `AGENTS.md` before `CLAUDE.md`. A link is
followed only when its target stays inside the level's root; a link that leaves it, or is broken,
is ignored, and no linked directory is ever entered. A line shaped like a secret is never read
into a hint. The detector is a local subset of
the one in the `pc-core` plugin, which this plugin cannot import without requiring it to be
installed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "hooks"))
from pc_memory_lib import (  # noqa: E402
    INDEX_NAME,
    LEDGER_NAME,
    Level,
    clean_ledger,
    clean_text,
    collect_levels,
    level_root,
    link_targets,
    note_age_days,
    short,
)

INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25 * 1024
TEXT_CHARS = 200
FRONTMATTER_LIMIT = 100
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
COVERAGE_MATCHES = 3
COVERAGE_TEXT_CHARS = 160
COVERAGE_FILES_PER_LEVEL = 60
COVERAGE_FILE_BYTES = 200 * 1024
COVERAGE_WINDOW = 3
COVERAGE_RARE_SHARE = 0.10
COVERAGE_COMMON_FLOOR = 3
WEIGHT_RARE = 10
WEIGHT_COMMON = 1
COVERAGE_MIN_SCORE = 4 * WEIGHT_RARE
COVERAGE_WORD_CHARS = 6
COVERAGE_STRONG_CHARS = 8
COVERAGE_WALK_DEPTH = 6
COVERAGE_WALK_DIRS = 4000
_SKIPPED_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".tox"}
)
_STOPWORDS = frozenset(
    "about above across always another because before being between cannot could during "
    "every feedback instead memory never other project reference should still their there "
    "these those through using which while without would claude session sessions".split()
)
_DOMAIN_WORDS = frozenset(
    "plugin plugins hooks hook skill skills scripts script working tree files file repo repos "
    "code claude session sessions memory note notes rule rules".split()
)
_BACKTICKED = re.compile(r"`([^`\n]{3,60})`")
_NAME_WORD = re.compile(r"[A-Za-z0-9]+")
_TEXT_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_DESCRIPTION_VALUE = re.compile(r'"description"\s*:\s*(".*")\s*,?\s*$')
_SETTING_VALUE = re.compile(r'^\s*(?:"[^"]*"\s*:\s*)?(".*")\s*,?\s*$')
_TOKEN = r"(?=[A-Za-z0-9_+/=.-]*\d)(?=[A-Za-z0-9_+/=.-]*[A-Za-z])[A-Za-z0-9_+/=.-]{20,}"
_SECRET_SHAPES = (
    re.compile(rf"[:=]\s*[\"']?{_TOKEN}"),
    re.compile(r"\b(?:gh[ps]_|xox[baprs]-|sk[-_]|AKIA)[-_A-Za-z0-9]{16,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"https?://[^/\s:@]+:[^/\s@]+@\S+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]+"),
    re.compile(
        r"\b(?=[A-Za-z0-9+]*[a-z])(?=[A-Za-z0-9+]*[A-Z])(?=[A-Za-z0-9+]*[0-9])"
        r"[A-Za-z0-9+]{32,}={0,2}\b"
    ),
)


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Flat `key: value` pairs of a leading `---` block, and the body that follows it."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields: dict[str, str] = {}
    for index, line in enumerate(lines[1 : FRONTMATTER_LIMIT + 1], start=1):
        if line.strip() == "---":
            return fields, "\n".join(lines[index + 1 :])
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith((" ", "\t")):
            fields[key.strip()] = value.strip().strip("\"'")
    return {}, text


def _text(value: str) -> str:
    """`value` without control characters, on one line, capped."""
    return " ".join(clean_text(value).split())[:TEXT_CHARS]


def _clean(value: str) -> str:
    """`value` without control characters, capped: a name or a path fragment, spaces kept."""
    return clean_text(value)[:TEXT_CHARS]


def _date(value: object) -> str | None:
    """`value` when it is a `YYYY-MM-DD` date string, else None."""
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            date.fromisoformat(value)
        except ValueError:
            return None
        return value
    return None


def _read_ledger(directory: Path) -> dict[str, object]:
    """Ledger of a memory directory, empty when absent or unusable. Never renames or writes."""
    try:
        return clean_ledger(
            json.loads((directory / LEDGER_NAME).read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return clean_ledger(None)


def _note_files(directory: Path) -> list[Path]:
    """Regular `*.md` files of a memory directory that are notes, sorted by name."""
    out: list[Path] = []
    for path in sorted(directory.glob("*.md")):
        if path.name == INDEX_NAME or path.name.startswith("."):
            continue
        if path.is_file():
            out.append(path)
    return out


def _index_report(directory: Path) -> tuple[dict[str, object], list[str]]:
    """Size report of the index and its lines."""
    raw = (directory / INDEX_NAME).read_bytes()
    lines = raw.decode("utf-8", errors="replace").splitlines()
    report: dict[str, object] = {
        "lines": len(lines),
        "bytes": len(raw),
        "over_budget": len(lines) > INDEX_MAX_LINES or len(raw) > INDEX_MAX_BYTES,
    }
    return report, lines


@dataclass(frozen=True)
class SourceLine:
    """A readable line of an instruction file: its number, its text and its casefolded text."""

    number: int
    text: str
    folded: str


@dataclass(frozen=True)
class Source:
    """An instruction file reduced to its readable lines."""

    path: Path
    lines: list[SourceLine]


@dataclass(frozen=True)
class Window:
    """The best run of lines of a source for a note: its score, first line, text and matched terms."""

    score: int
    line: int
    text: str
    terms: list[str]


def _secret_shaped(line: str) -> bool:
    """True when `line` looks like it holds a secret."""
    return any(shape.search(line) for shape in _SECRET_SHAPES)


def _json_text(value: str) -> str:
    """The string a JSON value stands for, the raw text when it does not parse."""
    try:
        parsed = json.loads(value)
    except ValueError:
        return value.strip('"')
    return parsed if isinstance(parsed, str) else ""


def _read_source(path: Path, kind: str, root: Path) -> Source | None:
    """Readable lines of one instruction file, None when it is not a small regular file of `root`.

    `kind` is `text` (every line), `settings` (each string value, as `"key": "value"` or as an array item) or `hooks`
    (the value of each `"description"` line). The file is read through its real path, which must
    stay inside `root` (a symlink that leaves it, or is broken, gives None). A file over the size
    cap, an unreadable file and a line shaped like a secret are left out. The source keeps the name
    `path`, as met.
    """
    real = Path(os.path.realpath(path))
    if not real.is_relative_to(os.path.realpath(root)):
        return None
    try:
        info = real.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > COVERAGE_FILE_BYTES:
            return None
        raw = real.read_bytes()
    except OSError:
        return None
    lines: list[SourceLine] = []
    for number, line in enumerate(
        raw.decode("utf-8", errors="replace").splitlines(), start=1
    ):
        if _secret_shaped(line):
            continue
        if kind == "hooks":
            found = _DESCRIPTION_VALUE.search(line)
            line = _json_text(found.group(1)) if found else ""
        elif kind == "settings":
            found = _SETTING_VALUE.match(line)
            line = _json_text(found.group(1)) if found else ""
        text = " ".join(clean_text(line).split())
        if text:
            lines.append(SourceLine(number, text, text.casefold()))
    return Source(path, lines)


def _walk_sources(root: Path, budget: int) -> list[tuple[Path, str]]:
    """Skill files and hook manifests below `root`, in sorted order, at most `budget` of them.

    Depth, directories visited and files kept are bounded. A symlinked directory is never entered;
    a symlink named like a source is kept as a candidate and checked when read.
    """
    found: list[tuple[Path, str]] = []
    pending: list[tuple[Path, int]] = [(root, 0)]
    visited = 0
    while pending and len(found) < budget and visited < COVERAGE_WALK_DIRS:
        directory, depth = pending.pop()
        visited += 1
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            continue
        subdirectories: list[Path] = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < COVERAGE_WALK_DEPTH and entry.name not in _SKIPPED_DIRS:
                        subdirectories.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) or entry.is_symlink():
                    if entry.name == "SKILL.md" and directory.parent.name == "skills":
                        found.append((Path(entry.path), "text"))
                    elif entry.name == "hooks.json" and directory.name == "hooks":
                        found.append((Path(entry.path), "hooks"))
            except OSError:
                continue
        pending.extend((sub, depth + 1) for sub in reversed(subdirectories))
    return found[:budget]


def _level_source_files(root: Path) -> list[tuple[Path, str]]:
    """The instruction files of one level that may exist, the fixed ones first."""
    files: list[tuple[Path, str]] = [
        (root / "AGENTS.md", "text"),
        (root / "CLAUDE.md", "text"),
    ]
    config = root / ".claude"
    if not config.is_symlink():
        rules = config / "rules"
        try:
            if not rules.is_symlink():
                files += [(path, "text") for path in sorted(rules.glob("*.md"))]
        except OSError:
            pass
        files.append((config / "settings.pinky.json", "settings"))
    return files + _walk_sources(root, COVERAGE_FILES_PER_LEVEL)


def collect_sources(roots: list[Path]) -> list[Source]:
    """Every readable instruction source below the level roots, each real file once.

    At most `COVERAGE_FILES_PER_LEVEL` files are read per level. When several names lead to one
    file, the first met is the one kept.
    """
    seen: set[str] = set()
    sources: list[Source] = []
    for root in roots:
        read = 0
        for path, kind in _level_source_files(root):
            if read >= COVERAGE_FILES_PER_LEVEL:
                break
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            source = _read_source(path, kind, root)
            if source is not None:
                read += 1
                if source.lines:
                    sources.append(source)
    return sources


def _terms(name: str, description: str) -> tuple[set[str], set[str]]:
    """Distinctive terms of a note and, among them, the strong ones (backticked, 8+ characters)."""
    quoted = {m.strip().casefold() for m in _BACKTICKED.findall(description)}
    words = {
        word.casefold()
        for word in [
            *_NAME_WORD.findall(Path(name).stem),
            *_TEXT_WORD.findall(description),
        ]
        if len(word) >= COVERAGE_WORD_CHARS
    }
    terms = (quoted | words) - _STOPWORDS - _DOMAIN_WORDS
    return terms, {
        term for term in quoted & terms if len(term) >= COVERAGE_STRONG_CHARS
    }


@dataclass(frozen=True)
class Hit:
    """The lines of a source that state a note's terms."""

    line: SourceLine
    found: list[str]


def _weights(
    terms: set[str], strong: set[str], hits: list[list[Hit]], lines_total: int
) -> dict[str, int]:
    """Weight of each term from how rare it is in the sources the note is checked against.

    A term found in more than `COVERAGE_RARE_SHARE` of the lines, or of the files, is common and
    weighs `WEIGHT_COMMON`; any other weighs `WEIGHT_RARE`. A strong term that is rare weighs the
    whole `COVERAGE_MIN_SCORE`. Tiny corpora keep a floor of `COVERAGE_COMMON_FLOOR` lines or files.
    """
    in_lines: Counter[str] = Counter()
    in_files: Counter[str] = Counter()
    for source_hits in hits:
        for hit in source_hits:
            in_lines.update(hit.found)
        in_files.update(set().union(*(hit.found for hit in source_hits)))
    line_limit = max(COVERAGE_COMMON_FLOOR, COVERAGE_RARE_SHARE * lines_total)
    file_limit = max(COVERAGE_COMMON_FLOOR, COVERAGE_RARE_SHARE * len(hits))
    weights: dict[str, int] = {}
    for term in terms:
        rare = in_lines[term] <= line_limit and in_files[term] <= file_limit
        if rare and term in strong:
            weights[term] = COVERAGE_MIN_SCORE
        else:
            weights[term] = WEIGHT_RARE if rare else WEIGHT_COMMON
    return weights


def _best_window(hits: list[Hit], weights: dict[str, int]) -> Window | None:
    """The best window of one source, None when none reaches `COVERAGE_MIN_SCORE`.

    A window spans `COVERAGE_WINDOW` lines from a line that holds a term; its score is the sum of
    the weights of the distinct terms it holds. The best score wins, the earliest on a tie.
    """
    best: Window | None = None
    for start, first in enumerate(hits):
        union: set[str] = set()
        for hit in hits[start:]:
            if hit.line.number - first.line.number >= COVERAGE_WINDOW:
                break
            union.update(hit.found)
        score = sum(weights[term] for term in union)
        if score >= COVERAGE_MIN_SCORE and (best is None or score > best.score):
            best = Window(
                score,
                first.line.number,
                first.line.text,
                sorted(term for term in union if weights[term] > 0),
            )
    return best


def _coverage(
    note: str, description: str, sources: list[Source]
) -> list[dict[str, object]]:
    """Up to `COVERAGE_MATCHES` places where the note's fact is already stated, best first."""
    terms, strong = _terms(note, description)
    if not terms:
        return []
    pattern = re.compile(
        r"(?<![A-Za-z0-9_])(?:"
        + "|".join(
            re.escape(term) for term in sorted(terms, key=lambda t: (-len(t), t))
        )
        + r")(?![A-Za-z0-9_])"
    )
    hits: list[list[Hit]] = []
    for source in sources:
        found = (
            (line, {m.group(0) for m in pattern.finditer(line.folded)})
            for line in source.lines
        )
        hits.append(
            [
                Hit(line, sorted(terms_found))
                for line, terms_found in found
                if terms_found
            ]
        )
    weights = _weights(
        terms, strong, hits, sum(len(source.lines) for source in sources)
    )
    scored: list[tuple[int, int, dict[str, object]]] = []
    for order, source in enumerate(sources):
        window = _best_window(hits[order], weights)
        if window is not None:
            scored.append(
                (
                    -window.score,
                    order,
                    {
                        "file": _clean(short(source.path)),
                        "line": window.line,
                        "text": window.text[:COVERAGE_TEXT_CHARS],
                        "terms": window.terms,
                    },
                )
            )
    scored.sort(key=lambda item: item[:2])
    return [match for _, _, match in scored[:COVERAGE_MATCHES]]


def _level_report(
    level: Level, current: Path, now: float, sources: list[Source]
) -> tuple[dict[str, object], list[tuple[str, str]]]:
    """Report of one level, and the (file, normalised body) pairs of its notes."""
    directory = level.memory_dir
    ledger = _read_ledger(directory)
    stored = ledger["notes"] if isinstance(ledger["notes"], dict) else {}
    index, lines = _index_report(directory)
    line_of: dict[str, str] = {}
    linked: set[str] = set()
    for line in lines:
        for target in link_targets(line):
            linked.add(target)
            line_of.setdefault(target, _text(line))
    notes: list[dict[str, object]] = []
    bodies: list[tuple[str, str]] = []
    names: set[str] = set()
    for path in _note_files(directory):
        text = path.read_text(encoding="utf-8", errors="replace")
        fields, body = _split_frontmatter(text)
        entry = stored.get(path.name)
        counted = entry if isinstance(entry, dict) else {}
        sessions = counted.get("sessions")
        shown = _clean(path.name)
        description = _text(fields.get("description", ""))
        names.add(path.name)
        bodies.append((shown, " ".join(body.split())))
        notes.append(
            {
                "file": shown,
                "description": description,
                "type": _text(fields.get("type", "")),
                "age_days": note_age_days(path, now),
                "sessions": sessions if isinstance(sessions, int) else 0,
                "last_read": _date(counted.get("last_read")),
                "in_index": path.name in linked,
                "index_line": line_of.get(path.name),
                "coverage": _coverage(path.name, description, sources),
            }
        )
    missing = sorted(
        _clean(t)
        for t in linked
        if t.endswith(".md")
        and t.casefold() != INDEX_NAME.casefold()
        and not (directory / t).is_file()
        and unquote(t) not in linked - {t}
    )
    report: dict[str, object] = {
        "level": short(level.root),
        "dir": short(directory),
        "role": "current" if level.root == current else "parent",
        "last_review": _date(ledger["last_review"]),
        "index": index,
        "notes": notes,
        "orphans": {
            "index_without_file": missing,
            "file_without_index": sorted(_clean(n) for n in names if n not in linked),
        },
    }
    return report, bodies


def build_inventory(cwd: Path, now: float) -> dict[str, object]:
    """Inventory of the levels of `cwd`: per-level reports and the exact duplicates.

    A level that cannot be read is left out with a message on stderr, the others are kept.

    Args:
        cwd: Working directory the levels are resolved from.
        now: Timestamp the note ages are computed against.

    Returns:
        The JSON-ready inventory.
    """
    current = level_root(cwd)
    levels: list[dict[str, object]] = []
    groups: dict[str, list[dict[str, str]]] = {}
    shown = collect_levels(cwd, trust_shared_project_settings=False)
    sources = collect_sources([level.root for level in shown])
    for level in shown:
        try:
            report, bodies = _level_report(level, current, now, sources)
        except OSError as exc:
            sys.stderr.write(f"inventory: {short(level.memory_dir)}: {exc}\n")
            continue
        levels.append(report)
        for name, body in bodies:
            if body:
                groups.setdefault(body, []).append(
                    {"level": short(level.root), "file": name}
                )
    exact = [sorted(g, key=lambda m: (m["level"], m["file"])) for g in groups.values()]
    return {
        "levels": levels,
        "duplicates": {
            "exact": sorted(
                (group for group in exact if len(group) > 1),
                key=lambda g: (g[0]["level"], g[0]["file"]),
            )
        },
    }


def main() -> int:
    """Entry point: print the inventory of `--cwd` (default: the working directory)."""
    parser = argparse.ArgumentParser(description="Inventory of memory notes, as JSON.")
    parser.add_argument("--cwd", default=None, help="directory the levels start from")
    args = parser.parse_args()
    cwd = Path(args.cwd if args.cwd else os.getcwd()).expanduser().resolve()
    print(json.dumps(build_inventory(cwd, time.time()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
