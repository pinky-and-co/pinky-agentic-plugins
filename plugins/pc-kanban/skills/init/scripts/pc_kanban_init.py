#!/usr/bin/env python3
"""`pc-kanban:init` — scaffold a board's `_KANBAN/INDEX.md` + nested `_AGENT_LOG/`, and record its
declared boards in `~/.claude/settings.pinky.json`.

Generic, no repo-specific default: `--workspace <path>` or the current directory. Idempotent — a
rerun on an already-scaffolded root changes nothing (`INDEX.md` is never overwritten;
`settings.pinky.json` is only rewritten when the merged `kanban_boards` value actually differs from
what is already there). Never scaffolds a first chantier — that is skill `grill`'s job.

Also migrates, once, whatever extra board root was already visible for this workspace via the
retired discovery mechanism (a `*.code-workspace`'s `folders[]`, or the `KANBAN_ROOTS` env var) —
union-merged into `kanban_boards[<root>]`, never dropping an entry already declared there.

Python 3.11+, stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SETTINGS_PINKY_PATH = Path.home() / ".claude" / "settings.pinky.json"

_MIGRATE_AGENT_LOG = (
    Path(__file__).resolve().parents[3]
    / "hooks"
    / "agent-log-migration"
    / "pc_kanban_migrate_agent_log.py"
)

_ROOTS_ENV = "KANBAN_ROOTS"
_CODE_WORKSPACE_GLOB = "*.code-workspace"

_CHANTIER_DIR_RE = re.compile(r"^CHANTIER(\d{4})-(.+)$")

LEGEND = (
    "**Chantier = objectif, pas un repo.** Statuts : 🟢 active · ⚪ dormant · 🔴 blocked · "
    "🟡 partial · ⛔ deprecated."
)


# ── Legacy discovery — only ever read here, for the one-time migration ───────────────────────


def _strip_jsonc_comments(text: str) -> str:
    """Strip `//` and `/* */` comments from JSONC, respecting string literals.

    `*.code-workspace` is JSONC (VSCode's own format), not strict JSON — real files carry `//`
    banners (path warnings, mount-point rationale) that `json.loads` chokes on. A regex would strip
    a `//` inside a Windows path string; this walks the text char-by-char instead.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _nearest_code_workspace(start: Path) -> Path | None:
    """Nearest `*.code-workspace` FILE above `start`, or None."""
    for base in [start, *start.parents]:
        matches = [p for p in base.glob(_CODE_WORKSPACE_GLOB) if p.is_file()]
        if matches:
            return matches[0]
    return None


def _parse_roots(raw: list[str], base: Path) -> list[Path]:
    """One dir per entry; blanks and `#` comments skipped; relative entries resolved against `base`."""
    roots: list[Path] = []
    for item in raw:
        item = item.strip()
        if not item or item.startswith("#"):
            continue
        p = Path(item).expanduser()
        roots.append(p if p.is_absolute() else base / p)
    return roots


def _workspace_folders(workspace_file: Path) -> list[Path]:
    """Every `folders[]` entry of `workspace_file`, resolved against its own directory — `[]` on
    any parse failure."""
    try:
        raw_text = _strip_jsonc_comments(workspace_file.read_text(encoding="utf-8"))
        data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError):
        return []
    folders = data.get("folders") if isinstance(data, dict) else None
    if not isinstance(folders, list):
        return []
    base = workspace_file.parent
    roots: list[Path] = []
    for entry in folders:
        raw = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(raw, str):
            continue
        p = Path(raw).expanduser()
        roots.append(p if p.is_absolute() else base / p)
    return roots


def legacy_declared_roots(root: Path) -> list[Path]:
    """Additional board roots visible for `root` via the retired discovery mechanism — env
    `KANBAN_ROOTS` (if set) else every `folders[]` entry of the nearest `*.code-workspace` above
    `root` — restricted to what that mechanism actually surfaced: an entry resolving to `root`
    itself is never its own additional board, and an entry with no `_KANBAN/` of its own was never
    surfaced either.

    Args:
        root: the workspace root being initialized.

    Returns:
        Resolved, deduplicated candidate roots, in discovery order.
    """
    env = os.environ.get(_ROOTS_ENV)
    if env:
        candidates = _parse_roots(env.split(os.pathsep), root)
    else:
        workspace_file = _nearest_code_workspace(root)
        candidates = _workspace_folders(workspace_file) if workspace_file else []
    resolved_root = root.resolve()
    seen: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved == resolved_root:
            continue
        if not (resolved / "_KANBAN").is_dir():
            continue
        if resolved not in seen:
            seen.append(resolved)
    return seen


# ── settings.pinky.json — kanban_boards ───────────────────────────────────────────────────────


def _load_settings() -> dict:
    """The whole parsed `SETTINGS_PINKY_PATH`, or `{}` when the file does not exist yet.

    Returns:
        The parsed JSON object, or `{}` when the file is simply absent.

    Raises:
        SystemExit: the file exists but is not valid JSON, or its top level is not an object —
            this file also carries `model_pool`/`keys`, so writing `kanban_boards` into that
            state would silently discard them. A human must look, not a script guessing.
    """
    if not SETTINGS_PINKY_PATH.is_file():
        return {}
    try:
        data = json.loads(SETTINGS_PINKY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"refusing: {SETTINGS_PINKY_PATH} exists but is not valid JSON ({exc}) — "
            "resolve by hand before running init"
        ) from exc
    if not isinstance(data, dict):
        raise SystemExit(
            f"refusing: {SETTINGS_PINKY_PATH} exists but its top level is not a JSON "
            "object — resolve by hand before running init"
        )
    return data


def _write_settings(data: dict) -> None:
    """Write `data` to `SETTINGS_PINKY_PATH` via a temp file + atomic rename — this file also
    holds API keys, so a crash mid-write must never leave it half-written."""
    SETTINGS_PINKY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PINKY_PATH.with_suffix(SETTINGS_PINKY_PATH.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(SETTINGS_PINKY_PATH)


def write_kanban_boards(root: Path) -> str:
    """Merge `legacy_declared_roots(root)` into `kanban_boards[<root>]` of `SETTINGS_PINKY_PATH` —
    union with whatever is already declared there, persisted only when a genuinely new entry
    needs adding. Already-stored strings are kept byte-for-byte (a hand-added `~/...` or relative
    entry is never re-anchored to whatever `root` happens to be at this call) — only the resolved
    form is used to decide whether a legacy entry is already represented. Every other top-level
    key (`model_pool`, `keys`, …) is read back untouched.

    Args:
        root: the workspace root being initialized, used as the resolved `kanban_boards` key.

    Returns:
        A one-line summary of what changed, or that nothing did.
    """
    resolved_root = root.resolve()
    key = str(resolved_root)
    data = _load_settings()
    boards = data.get("kanban_boards")
    boards = dict(boards) if isinstance(boards, dict) else {}
    key_present = key in boards
    existing_raw = boards.get(key)
    existing = (
        [p for p in existing_raw if isinstance(p, str)]
        if isinstance(existing_raw, list)
        else []
    )
    existing_resolved = {str(Path(p).resolve()) for p in existing}
    new_legacy = sorted(
        {str(p) for p in legacy_declared_roots(resolved_root)} - existing_resolved
    )
    if key_present and not new_legacy:
        return f"kanban_boards[{key}] unchanged ({len(existing)} board(s))"
    merged = existing + new_legacy
    boards[key] = merged
    data["kanban_boards"] = boards
    _write_settings(data)
    return f"kanban_boards[{key}] = {merged}"


# ── INDEX.md ───────────────────────────────────────────────────────────────────────────────────


def existing_chantiers(kanban: Path) -> list[tuple[str, str]]:
    """`(id, slug)` for every `CHANTIER{NNNN}-<slug>/` directory already on disk, id-sorted — what
    `init` recovers into a freshly generated `INDEX.md` so a pre-existing chantier is never
    rendered invisible by an empty table.

    Args:
        kanban: the board's `_KANBAN/` root.

    Returns:
        Sorted `(id, slug)` pairs, one per matching directory.
    """
    rows: list[tuple[str, str]] = []
    for d in kanban.glob("CHANTIER*"):
        if not d.is_dir():
            continue
        m = _CHANTIER_DIR_RE.match(d.name)
        if m:
            rows.append((m.group(1), m.group(2)))
    return sorted(rows)


def render_index(chantiers: list[tuple[str, str]]) -> str:
    """The minimal generated `INDEX.md` — frontmatter, one H1, a `## Board` section carrying the
    legend line and the table (header alone when `chantiers` is empty, one dormant row per entry
    otherwise). Deliberately excludes every workspace-specific section (conventions, product
    topology) a hand-maintained board accretes over time — those are that installer's own prose,
    not a template to copy forward.

    Args:
        chantiers: `(id, slug)` pairs, as returned by `existing_chantiers`.

    Returns:
        The full `INDEX.md` text, trailing newline included.
    """
    lines = [
        "---",
        "title: KANBAN",
        "---",
        "# _KANBAN — board de chantiers",
        "",
        "## Board",
        "",
        LEGEND,
        "",
        "| # | Chantier (objectif) | Statut |",
        "|---|---------------------|--------|",
    ]
    for chantier_id, slug in chantiers:
        lines.append(f"| {chantier_id} | {slug} | ⚪ |")
    return "\n".join(lines) + "\n"


# ── _AGENT_LOG/ ────────────────────────────────────────────────────────────────────────────────


def _migrate_agent_log(kanban: Path) -> str:
    """Run `pc_kanban_migrate_agent_log.py --kanban <kanban>` — folds a pre-existing sibling
    `_AGENT_LOG/` into the nested one instead of shadowing it with a blank directory.

    The script itself missing/unable to launch is tolerated (nothing to migrate from either way,
    safe to let the caller's own `mkdir` land an empty nested dir) — but a nonzero exit from a
    script that DID run means it detected a real problem (both paths real, a foreign symlink, an
    interrupted rename) and refused; silently proceeding to `mkdir` an empty nested dir right next
    to a still-populated sibling would create exactly the split-journal state this exists to
    prevent.

    Args:
        kanban: the board's `_KANBAN/` root, already created.

    Returns:
        The migration script's own one-line summary, or a note that it was skipped.

    Raises:
        SystemExit: the migration script ran and refused — never silently swallowed.
    """
    try:
        out = subprocess.run(
            [sys.executable, str(_MIGRATE_AGENT_LOG), "--kanban", str(kanban)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"agent-log migration skipped ({exc})"
    if out.returncode != 0:
        raise SystemExit(
            "refusing: agent-log migration failed "
            f"({out.stderr.strip() or out.stdout.strip()}) — resolve by hand, init will not "
            "create an empty _AGENT_LOG/ next to a sibling it could not fold in"
        )
    return out.stdout.strip() or "agent-log migration: no output"


# ── orchestration ──────────────────────────────────────────────────────────────────────────────


def _ancestor_kanban(root: Path) -> Path | None:
    """The nearest existing `_KANBAN/` among `root`'s ancestors (never `root` itself).

    A hit means `root` is nested under an already-initialized board — scaffolding a second
    `_KANBAN/` here would permanently shadow the real one for every session under this subtree,
    since the read side (`find_kanbans`) always walks up and stops at the nearest one.

    Args:
        root: the workspace root about to be initialized.

    Returns:
        The ancestor `_KANBAN/` directory found, or None.
    """
    for parent in root.parents:
        candidate = parent / "_KANBAN"
        if candidate.is_dir():
            return candidate
    return None


def init(root: Path) -> str:
    """Scaffold `root`'s board — `_KANBAN/INDEX.md` + nested `_AGENT_LOG/` — and record its
    declared boards in `settings.pinky.json`. No-op on whatever already exists; never overwrites
    an `INDEX.md` already there, never scaffolds a first chantier.

    Args:
        root: the workspace root to initialize.

    Returns:
        A multi-line summary of what was created, migrated, or left untouched.

    Raises:
        SystemExit: an ancestor of `root` already boards this workspace — initializing here
            would create a second, nested board that shadows it, never guessed past.
    """
    root = root.resolve()
    ancestor = _ancestor_kanban(root)
    if ancestor is not None:
        raise SystemExit(
            f"refusing: {ancestor} already boards this workspace from above {root} — "
            "initializing here would create a second, nested board that shadows it"
        )
    messages: list[str] = []

    kanban = root / "_KANBAN"
    created_kanban = not kanban.is_dir()
    kanban.mkdir(parents=True, exist_ok=True)
    messages.append(
        f"created {kanban}" if created_kanban else f"{kanban} already present"
    )

    messages.append(_migrate_agent_log(kanban))
    agent_log = kanban / "_AGENT_LOG"
    if not agent_log.is_dir():
        agent_log.mkdir(parents=True)
        messages.append(f"created {agent_log}")

    index = kanban / "INDEX.md"
    if index.is_file():
        messages.append(f"{index} already present — left untouched")
    else:
        chantiers = existing_chantiers(kanban)
        index.write_text(render_index(chantiers), encoding="utf-8")
        messages.append(
            f"created {index} ({len(chantiers)} chantier(s) recovered from disk)"
        )

    messages.append(write_kanban_boards(root))
    return "\n".join(messages)


def main(argv: list[str]) -> int:
    """CLI entry point: `pc_kanban_init.py [--workspace <path>]`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="workspace root to scaffold; defaults to the current directory",
    )
    args = parser.parse_args(argv)
    root = (args.workspace or Path.cwd()).resolve()
    print(init(root))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
