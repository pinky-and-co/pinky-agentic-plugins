#!/usr/bin/env python3
"""pc_kanban_spawn_workers.py — one worker session per repo of a kanban card.

The orchestrator session (opened outside any git repo, board curation only) runs this to hand a
selected card to worker sessions: one per entry of the card's `repos:` frontmatter, each scoped to
the local clone, its Claude session named `<repo>-<slug>` (the workspace adopts that name), booting
straight into `/pc-kanban:chantier <id> <slug>` on the card's declared `model:` — with an
`--advisor` one tier above (never for `opus`/`fable`, already the ceiling).

Per repo, before opening the worker: `enable_project_plugins` turns on — at project scope, in the
target repo's *versioned* `.claude/settings.json` — every installed project-scoped plugin whose
`projectPath` covers the repo, so the worker's boot `/pc-kanban:chantier` runs with them live and
no `/reload-plugins` (FR-0025-52/53). Then:

- **Inside cmux / wmux** (`CMUX_SURFACE_ID` / `WMUX_SURFACE_ID`): `<mux> new-workspace --cwd <path>
  --command '<claude ...>'`, one per repo.
- **Outside any multiplexer**: prints a `cd <path> && claude ...` line per repo for the user to run,
  and spawns nothing.

No dependency on the `pinky` CLI (FR-0025-20): card lookup, board resolution, `repos:` and `model:`
all go through `stop_handoff.py`'s CLI (same plugin, same pattern guard.py uses); slug->path
resolution reuses `resolve_repos.sh` (sibling skill); the project-scope enable shells out to
`claude plugin` directly. The workspace root is derived from the board that holds the card — never
a configured value.

Multi-repo cards (FR-0025-21) are sequential by convention: pass the single next `<repo-slug>` to
spawn just that one. With no slug filter every repo of the card is spawned, in `repos:` order.

`--dry-run` previews the command line(s) and does nothing else — no spawn, no plugin enable.

Python 3.11+, stdlib only.

Usage: pc_kanban_spawn_workers.py [--dry-run] <chantier-id> <card-slug> [<repo-slug>...]
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pc_kanban_spawn_plugin_scope_lib import matching_plugin_ids

_USAGE = "Usage: pc_kanban_spawn_workers.py [--dry-run] <chantier-id> <card-slug> [<repo-slug>...]"

# `slug` and `chantier_id` end up inside the shell text the multiplexer types into a fresh terminal
# (`--command`) and inside a `glob()` pattern — constrain both to what a real card name uses so
# neither a metacharacter nor a quote can ride through.
_ID_RE = re.compile(r"[0-9]{3,4}")
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# the card `model:` alias -> the `claude --model` value the worker boots on. The card decides the
# tier, verbatim — a `haiku` card runs on haiku, not "at least haiku". `[1m]` is the long-context
# variant, used wherever it exists so a big card does not compact mid-work; `haiku` has none (the
# `haiku[1m]` alias is a hard 400, measured 2026-09-09). `fable[1m]` is assumed (fable is
# 1M-native) — if it 400s like haiku's, `claude` prints the error in the fresh terminal and the
# worker never starts: a loud failure, not a silent wrong model. A value not in this map is a
# malformed card and aborts the spawn before any command is built.
_MODEL_ARG = {
    "haiku": "haiku",
    "sonnet": "sonnet[1m]",
    "opus": "opus[1m]",
    "fable": "fable[1m]",
}
# advisor = one model tier above the card's model; `opus` and `fable` are already the ceiling.
_ADVISOR_ABOVE = {"haiku": "sonnet", "sonnet": "opus"}

_HOOK = Path(__file__).resolve().parents[3] / "hooks" / "stop_handoff.py"
_RESOLVE_REPOS = (
    Path(__file__).resolve().parents[2] / "chantier" / "scripts" / "resolve_repos.sh"
)


class SpawnTarget(NamedTuple):
    """A repo slug paired with the local clone path resolved for it."""

    slug: str
    path: Path


def multiplexer() -> str | None:
    """The terminal multiplexer this session runs under: 'cmux', 'wmux', or None.

    Returns:
        The binary name, keyed off `CMUX_SURFACE_ID` / `WMUX_SURFACE_ID`; None outside both.
    """
    if os.environ.get("CMUX_SURFACE_ID"):
        return "cmux"
    if os.environ.get("WMUX_SURFACE_ID"):
        return "wmux"
    return None


def model_arg(card_model: str) -> str | None:
    """The `claude --model` value for a card whose `model:` is `card_model` — or None if none.

    Args:
        card_model: the card's `model:` alias (`haiku` / `sonnet` / `opus` / `fable`), or "".

    Returns:
        `haiku` / `sonnet[1m]` / `opus[1m]` / `fable[1m]` — the card's tier as its long-context
        variant where one exists. None for "" or an unknown alias (no `--model` passed).
    """
    return _MODEL_ARG.get(card_model)


def advisor_for(model: str) -> str | None:
    """The `--advisor` model for a card whose floor is `model` — one tier up, or None at the top.

    Args:
        model: the card's `model:` alias (`haiku` / `sonnet` / `opus` / `fable`), or "".

    Returns:
        `sonnet` for a `haiku` card, `opus` for a `sonnet` card; None for `opus`, `fable`,
        an empty string, or any unknown alias.
    """
    return _ADVISOR_ABOVE.get(model)


def _hook(*args: str, allow_empty: bool = False) -> str:
    """Run a `stop_handoff.py` CLI mode and return its stdout, aborting on a non-zero exit.

    Args:
        args: the mode flag and its operands, e.g. ("--card-path", "0025", "my-slug").
        allow_empty: accept empty stdout as a valid answer (a card with no `repos:`/`model:`).

    Returns:
        The command's stdout, stripped of the trailing newline.

    Raises:
        SystemExit: the hook exited non-zero, or returned nothing while a value was
            expected — an installed copy predating these CLI modes exits 0 with no output
            rather than erroring, and that skew must be loud, not a downstream traceback.
    """
    proc = subprocess.run(
        [sys.executable, str(_HOOK), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"spawn-workers: {proc.stderr.strip()}")
    out = proc.stdout.rstrip("\n")
    if not out and not allow_empty:
        raise SystemExit(
            f"spawn-workers: `stop_handoff.py {args[0]}` returned nothing — the installed "
            "pc-kanban is likely stale (run: pinky install --plugin pc-kanban --force)"
        )
    return out


def board_workspace_root(card_path: Path) -> Path:
    """The workspace root a card's clones live under — the parent of its `_KANBAN` ancestor.

    Args:
        card_path: absolute path to the card file, under `<root>/_KANBAN/CHANTIER*/...`.

    Returns:
        The `<root>` directory.

    Raises:
        SystemExit: the path has no `_KANBAN` ancestor.
    """
    for parent in card_path.parents:
        if parent.name == "_KANBAN":
            return parent.parent
    raise SystemExit(f"spawn-workers: {card_path} is not under a _KANBAN board")


def select_repos(declared: list[str], wanted: list[str]) -> list[str]:
    """Filter a card's declared repos to those asked for, preserving declared order.

    Args:
        declared: the card's `repos:` slugs, in frontmatter order.
        wanted: repo slugs passed on the command line; empty means all declared.

    Returns:
        The declared slugs to spawn, in declared order.

    Raises:
        SystemExit: a wanted slug is not one of the card's declared repos.
    """
    if not wanted:
        return declared
    unknown = [w for w in wanted if w not in declared]
    if unknown:
        raise SystemExit(
            f"spawn-workers: {unknown} not in the card's repos: {declared}"
        )
    return [d for d in declared if d in wanted]


def resolve_paths(workspace_root: Path, slugs: list[str]) -> list[SpawnTarget]:
    """Resolve each org/repo slug to its local clone path via resolve_repos.sh.

    Args:
        workspace_root: root the bounded `.git` scan walks from.
        slugs: org/repo slugs to resolve, in order.

    Returns:
        One SpawnTarget per slug, in the same order.

    Raises:
        SystemExit: resolve_repos.sh reported 0 or >1 clones for a slug.
    """
    proc = subprocess.run(
        ["bash", str(_RESOLVE_REPOS), str(workspace_root), *slugs],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"spawn-workers: {proc.stderr.strip()}")
    paths = [line for line in proc.stdout.splitlines() if line.strip()]
    if len(paths) != len(slugs):
        raise SystemExit(
            f"spawn-workers: resolve_repos.sh returned {len(paths)} paths for {len(slugs)} slugs"
        )
    return [SpawnTarget(slug, Path(p)) for slug, p in zip(slugs, paths, strict=True)]


def build_claude_command(
    name: str, chantier_id: str, slug: str, card_model: str
) -> str:
    """The `claude` invocation a worker session boots with, as one shell string.

    Args:
        name: the session display name, `<repo>-<slug>`.
        chantier_id: the four-digit chantier number.
        slug: the card filename without ".md".
        card_model: the card's `model:` alias, or "" — `--model` / `--advisor` are derived
            from it (`model_arg`, `advisor_for`), not passed as-is.

    Returns:
        One POSIX shell string — for a sonnet card:
        claude --name pinky-bootstrap-fix --model "sonnet[1m]" --advisor "opus" "/pc-kanban:chantier 0025 fix"
    """
    # A multiplexer types this line into a fresh shell. `name` holds an unconstrained repo
    # basename → shlex.quote (single-quote) neutralises `$`/backtick/quote. Everything else —
    # the `[1m]` model variant (a glob bracket zsh would choke on unquoted), the advisor alias,
    # the slash-command arg — is a value from our own literal maps or a regex-validated id/slug,
    # so a plain double-quote is enough and keeps the line readable inside an outer shlex.join.
    parts = ["claude", "--name", shlex.quote(name)]
    model = model_arg(card_model)
    if model:
        parts += ["--model", f'"{model}"']
    advisor = advisor_for(card_model)
    if advisor:
        parts += ["--advisor", f'"{advisor}"']
    parts.append(f'"/pc-kanban:chantier {chantier_id} {slug}"')
    return " ".join(parts)


def build_spawn_command(
    mux: str, target: SpawnTarget, chantier_id: str, slug: str, card_model: str
) -> list[str]:
    """Build the `<mux> new-workspace` argv for one repo of a card.

    Args:
        mux: the multiplexer binary, 'cmux' or 'wmux'.
        target: the repo slug and its resolved local path.
        chantier_id: the four-digit chantier number.
        slug: the card filename without ".md".
        card_model: the card's `model:` alias.

    Returns:
        The full argv list, ready for subprocess.run without a shell. The workspace name is
        left to the multiplexer, which adopts the `claude --name` value.
    """
    name = f"{target.slug.split('/')[-1]}-{slug}"
    inner = build_claude_command(name, chantier_id, slug, card_model)
    return [mux, "new-workspace", "--cwd", str(target.path), "--command", inner]


def build_manual_line(
    target: SpawnTarget, chantier_id: str, slug: str, card_model: str
) -> str:
    """The `cd <path> && claude ...` line a user runs when no multiplexer is available.

    Args:
        target: the repo slug and its resolved local path.
        chantier_id: the four-digit chantier number.
        slug: the card filename without ".md".
        card_model: the card's `model:` alias.

    Returns:
        One shell line — the repo path (shell-quoted), then `&&`, then the build_claude_command
        string.
    """
    name = f"{target.slug.split('/')[-1]}-{slug}"
    claude = build_claude_command(name, chantier_id, slug, card_model)
    return f"cd {shlex.quote(str(target.path))} && {claude}"


def enable_project_plugins(repo: Path) -> list[str]:
    """Enable, at project scope in `repo`, every installed project-scoped plugin whose
    `projectPath` covers `repo` — so a worker session booting there has them live without
    `/reload-plugins`. Writes the versioned `repo/.claude/settings.json` (`claude plugin
    install --scope project`), never `settings.local.json`.

    Each call is bounded and tried once: a failure is reported to stderr and skipped, never
    retried — the worker's own `/reload-plugins` and the always-on `pc-autodiscovery` hook
    stay as the fallback.

    Args:
        repo: absolute path to the target repo's worktree root.

    Returns:
        The plugin ids newly enabled by this call, in discovery order.
    """
    enabled: list[str] = []
    for plugin_id in matching_plugin_ids(repo):
        try:
            proc = subprocess.run(
                [
                    "claude",
                    "plugin",
                    "install",
                    plugin_id,
                    "--scope",
                    "project",
                    "--yes",
                ],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(
                f"spawn-workers: enabling {plugin_id} in {repo}: {exc}", file=sys.stderr
            )
            continue
        if proc.returncode == 0:
            enabled.append(plugin_id)
        else:
            print(
                f"spawn-workers: enabling {plugin_id} in {repo}: {proc.stderr.strip()}",
                file=sys.stderr,
            )
    return enabled


def _report_enabled(target: SpawnTarget, enabled: list[str]) -> None:
    """Print a one-line summary of the plugins `enable_project_plugins` turned on for `target`."""
    if enabled:
        print(f"enabled -s project in {target.slug}: {', '.join(enabled)}")


def main(argv: list[str]) -> int:
    """Parse args, resolve the card, then spawn (or print) one worker per repo.

    Args:
        argv: command-line arguments without the program name.

    Returns:
        Process exit code.

    Raises:
        SystemExit: the id/slug pair is malformed, the card is missing or ambiguous, a repo
            slug is not one the card declares, resolve_repos.sh failed, or the card declares
            no repos at all.
    """
    dry_run = "--dry-run" in argv
    positional = [a for a in argv if a != "--dry-run"]
    if len(positional) < 2:
        print(_USAGE, file=sys.stderr)
        return 2
    chantier_id, slug, *wanted = positional
    if not _ID_RE.fullmatch(chantier_id) or not _SLUG_RE.fullmatch(slug):
        raise SystemExit(
            f"spawn-workers: '{chantier_id} {slug}' is not a <digits> <card-slug> pair"
        )

    card_path = Path(_hook("--card-path", chantier_id, slug))
    declared = [
        s
        for s in _hook("--card-repos", str(card_path), allow_empty=True).splitlines()
        if s
    ]
    if not declared:
        raise SystemExit(
            f"spawn-workers: card '{slug}' declares no repos: — nothing to spawn"
        )
    card_model = _hook("--card-model", str(card_path), allow_empty=True)
    if card_model and card_model not in _MODEL_ARG:
        raise SystemExit(
            f"spawn-workers: card '{slug}' has model: {card_model!r} — "
            f"not one of {sorted(_MODEL_ARG)}"
        )

    slugs = select_repos(declared, wanted)
    targets = resolve_paths(board_workspace_root(card_path), slugs)
    mux = multiplexer()

    if mux is None:
        print(
            f"# no cmux/wmux here — open a worker session yourself in each of the "
            f"{len(targets)} repo(s):"
        )
        for target in targets:
            if not dry_run:
                _report_enabled(target, enable_project_plugins(target.path))
            print(build_manual_line(target, chantier_id, slug, card_model))
        return 0

    for target in targets:
        if not dry_run:
            _report_enabled(target, enable_project_plugins(target.path))
        command = build_spawn_command(mux, target, chantier_id, slug, card_model)
        if dry_run:
            print(shlex.join(command))
            continue
        subprocess.run(command, check=True)
        print(f"spawned {target.slug.split('/')[-1]}-{slug} → {target.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
