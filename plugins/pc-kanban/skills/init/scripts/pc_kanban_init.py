#!/usr/bin/env python3
"""`pc-kanban:init` — scaffold a board's `_KANBAN/INDEX.md` + nested `_AGENT_LOG/`, declare it in
`~/.claude/settings.pinky.json` (`kanban_boards`), and grant every declared board native
read/write access via `~/.claude/settings.json` (`permissions.additionalDirectories`).

Generic, no repo-specific default: `--workspace <path>` or the current directory. Idempotent — a
rerun on an already-scaffolded root changes nothing beyond filling in what a directly-edited
`kanban_boards` entry newly needs in `additionalDirectories`. Never scaffolds a first chantier —
that is skill `grill`'s job.

`init` reads nothing outside `_KANBAN`'s own tree and the two settings files above — no
`*.code-workspace`, no environment variable. An additional board for an already-initialized root
is a hand edit to `kanban_boards[<root>]`, the same convention `model_pool`/`keys` already use in
that same file; the next `init` run then grants it in `additionalDirectories` on its own.

Python 3.11+, stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SETTINGS_PINKY_PATH = Path.home() / ".claude" / "settings.pinky.json"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

_MIGRATE_AGENT_LOG = (
    Path(__file__).resolve().parents[3]
    / "hooks"
    / "agent-log-migration"
    / "pc_kanban_migrate_agent_log.py"
)

_CHANTIER_DIR_RE = re.compile(r"^CHANTIER(\d{4})-(.+)$")

LEGEND = (
    "**Chantier = a goal, not a repo.** Statuses: 🟢 active · ⚪ dormant · 🔴 blocked · "
    "🟡 partial · ⛔ deprecated."
)


# ── Shared JSON read/write — atomic, refuses a malformed file rather than guessing ────────────


def _load_json_object(path: Path, *, purpose: str) -> dict:
    """The whole parsed `path`, or `{}` when it does not exist yet.

    Args:
        path: the JSON file to read.
        purpose: one phrase naming what else this file carries, for the refusal message.

    Returns:
        The parsed JSON object, or `{}` when the file is simply absent.

    Raises:
        SystemExit: the file exists but is not valid JSON, or its top level is not an object —
            never guessed past, since a blind rewrite would silently discard `purpose`.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"refusing: {path} exists but is not valid JSON ({exc}) — it also carries "
            f"{purpose}, resolve by hand"
        ) from exc
    if not isinstance(data, dict):
        raise SystemExit(
            f"refusing: {path} exists but its top level is not a JSON object — it also "
            f"carries {purpose}, resolve by hand"
        )
    return data


def _write_json_object(path: Path, data: dict) -> None:
    """Write `data` to `path` via a temp file + atomic rename — both files this module writes
    also hold live harness/secrets config, so a crash mid-write must never leave either
    half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def write_kanban_boards(root: Path) -> str:
    """Ensure `kanban_boards[<root>]` exists in `SETTINGS_PINKY_PATH` — an empty list on a fresh
    root, left untouched if already present (an additional board declared there by hand since
    the last `init` is never overwritten). Every other top-level key (`model_pool`, `keys`, …) is
    read back untouched.

    Args:
        root: the workspace root being initialized, used as the resolved `kanban_boards` key.

    Returns:
        A one-line summary of what changed, or that nothing did.
    """
    resolved_root = root.resolve()
    key = str(resolved_root)
    data = _load_json_object(SETTINGS_PINKY_PATH, purpose="model_pool/keys")
    boards = data.get("kanban_boards")
    boards = dict(boards) if isinstance(boards, dict) else {}
    if key in boards:
        existing = boards[key] if isinstance(boards[key], list) else []
        return f"kanban_boards[{key}] unchanged ({len(existing)} board(s))"
    boards[key] = []
    data["kanban_boards"] = boards
    _write_json_object(SETTINGS_PINKY_PATH, data)
    return f"kanban_boards[{key}] = []"


def write_additional_directories(root: Path) -> str:
    """Grant native read/write access to `root`'s own `_KANBAN/` and every board already declared
    for it in `kanban_boards` — `permissions.additionalDirectories` in `CLAUDE_SETTINGS_PATH`.
    Union only: an entry already there (this mechanism's own, or anything else) is never removed,
    and every other top-level key (`hooks`, `enabledPlugins`, `model`, …) is read back untouched.

    Args:
        root: the workspace root just initialized (its `kanban_boards` key).

    Returns:
        A one-line summary of what changed, or that nothing did.
    """
    resolved_root = root.resolve()
    pinky_data = _load_json_object(SETTINGS_PINKY_PATH, purpose="model_pool/keys")
    declared = pinky_data.get("kanban_boards")
    extra_raw = declared.get(str(resolved_root)) if isinstance(declared, dict) else None
    extra = (
        [p for p in extra_raw if isinstance(p, str)]
        if isinstance(extra_raw, list)
        else []
    )
    wanted = {str(resolved_root / "_KANBAN")} | {
        str(Path(p).resolve() / "_KANBAN") for p in extra
    }

    data = _load_json_object(
        CLAUDE_SETTINGS_PATH, purpose="the harness's own live settings"
    )
    permissions = data.get("permissions")
    permissions = dict(permissions) if isinstance(permissions, dict) else {}
    existing_raw = permissions.get("additionalDirectories")
    existing = (
        [p for p in existing_raw if isinstance(p, str)]
        if isinstance(existing_raw, list)
        else []
    )
    missing = sorted(wanted - set(existing))
    if not missing:
        return f"additionalDirectories already covers {len(wanted)} board dir(s)"
    permissions["additionalDirectories"] = existing + missing
    data["permissions"] = permissions
    _write_json_object(CLAUDE_SETTINGS_PATH, data)
    return f"additionalDirectories += {missing}"


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
        "# _KANBAN — chantier board",
        "",
        "## Board",
        "",
        LEGEND,
        "",
        "| # | Chantier (objective) | Status |",
        "|---|-----------------------|--------|",
    ]
    for chantier_id, slug in chantiers:
        lines.append(f"| {chantier_id} | {slug} | ⚪ |")
    return "\n".join(lines) + "\n"


def _migrate_agent_log(kanban: Path) -> str:
    """Run `pc_kanban_migrate_agent_log.py --kanban <kanban>` — folds a pre-existing sibling
    `_AGENT_LOG/` into the nested one instead of shadowing it with a blank directory.

    The script itself missing/unable to launch is tolerated (nothing to migrate from either way,
    safe to let the caller's own `mkdir` land an empty nested dir) — but a nonzero exit from a
    script that DID run means it detected a real problem (both paths real, an unexpected file
    where a directory was expected) and refused; silently proceeding to `mkdir` an empty nested
    dir right next to a still-populated sibling would create exactly the split-journal state this
    exists to prevent.

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
    """Scaffold `root`'s board — `_KANBAN/INDEX.md` + nested `_AGENT_LOG/` — declare it in
    `settings.pinky.json`, and grant native access in `settings.json`. No-op on whatever already
    exists; never overwrites an `INDEX.md` already there, never scaffolds a first chantier.

    Args:
        root: the workspace root to initialize.

    Returns:
        A multi-line summary of what was created, declared, granted, or left untouched.

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
    messages.append(write_additional_directories(root))
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
