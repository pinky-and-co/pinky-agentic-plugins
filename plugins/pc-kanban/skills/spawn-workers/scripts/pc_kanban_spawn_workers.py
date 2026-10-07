#!/usr/bin/env python3
"""pc_kanban_spawn_workers.py — hand one kanban card, per repo, to a `card-worker` agent.

Run by the orchestrator session to resolve a card (its `repos:`, `model:` and each repo's local
clone). For each target repo it prints the two-block worker prompt (`build_worker_prompt`) and the
resolved model tier (`effective_model`), which the orchestrator passes to an
`Agent(subagent_type: "card-worker", ...)` call. The script never calls that tool itself.

The prompt embeds the repo's conventions (test location, gates/test commands, probed from
disk/PATH), the card's absolute path, its repo, and any judgment call to resolve.

`--cmux` opens a real `claude` session per repo instead of a subagent, for repos whose environment
does not reduce to commands (project-scoped plugin, scoped MCP server):
- Inside cmux/wmux (`CMUX_SURFACE_ID` / `WMUX_SURFACE_ID`): `<mux> new-workspace --cwd <path>
  --command '<claude ...>'`, booting `/pc-kanban:chantier <id> <slug>` on the card's `model:`
  (`--advisor` one tier above, never for `opus`/`fable`).
- Outside a multiplexer: prints a `cd <path> && claude ...` line per repo, spawns nothing.
In this mode, project-scoped plugins covering the repo are first enabled in its versioned
`.claude/settings.json`, so the session boots with them live.

Multi-repo cards are sequential: pass a single `<repo-slug>` to spawn only that repo; without
a slug, every repo is spawned in `repos:` order.

`--model <tier>` overrides the card's `model:` and `--arbitrage <text>` passes a judgment call to
the worker; both are call-time only. `--dry-run` only affects `--cmux` (previews the command
and skips the plugin enable).

Python 3.11+, stdlib only.

A card's optional `mockup:` (absolute or `~/` path to a file, folder or `.zip`) and
`mockup_fidelity:` (`axe=niveau` items; an absent axis is `guided`) add a mockup brief to the
default path's prompt; a `.zip` is unpacked into its sibling folder `<zip stem>/` (created if
absent, reused otherwise). Mockup handling is default-path only: `--cmux` neither reads the
mockup nor unpacks the zip.

`--effort <level>` overrides the card's optional `effort:` (`low`, `medium`, `high`, `xhigh`,
`max`); the default path prints `effort: <level>` only when one is set, and `--cmux` adds
`claude --effort <level>`.

Usage: pc_kanban_spawn_workers.py [--dry-run] [--cmux] [--model <tier>] [--effort <level>]
                                   [--arbitrage <text>] <chantier-id> <card-slug> [<repo-slug>...]
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pc_kanban_spawn_plugin_scope_lib import matching_plugin_ids

# Le parseur des lignes d'exigences est partagé avec `chantier` : il vit dans un dossier de concern.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "hooks" / "requirement-rows")
)
from pc_kanban_requirement_rows import (  # noqa: E402
    cited_ids,
    defined_rows,
    frontmatter,
    read_open_cards,
    requirement_rows,
)

_USAGE = (
    "Usage: pc_kanban_spawn_workers.py [--dry-run] [--cmux] [--model <tier>] "
    "[--effort <level>] [--arbitrage <text>] <chantier-id> <card-slug> [<repo-slug>...]"
)

# The worker's own role invariants live in the `card-worker` agent definition, not here — this is
# the one behavioural fact that is about the repo (a branch guard is real), not about the role.
_WORKER_INVARIANTS = (
    "Work on a dedicated branch (`git checkout -b feature/<name>`) before any edit — a hook denies "
    "a write on this repo's permanent branch. Never commit, push, or open a PR/MR; leave the work "
    "in the tree."
)

# `slug` and `chantier_id` end up inside the shell text the multiplexer types into a fresh terminal
# (`--command`) and inside a `glob()` pattern — constrain both to what a real card name uses so
# neither a metacharacter nor a quote can ride through.
_ID_RE = re.compile(r"[0-9]{3,4}")
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

_MODEL_ARG = {
    "haiku": "haiku",
    "sonnet": "sonnet[1m]",
    "opus": "opus[1m]",
    "fable": "fable[1m]",
}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
AXES = ("layout", "design", "content", "behavior")
LEVELS = ("strict", "guided", "free")
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
            "pc-kanban is likely stale (run: pinky claude-plugin install pc-kanban --force)"
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


def repo_test_location(repo: Path) -> str:
    """Where `repo`'s Python tests live, probed from disk — never retyped from memory.

    Args:
        repo: absolute path to the target repo's worktree root.

    Returns:
        "`tests/` at the repo root" when that directory exists, else a fallback naming the
        convention it is not.
    """
    if (repo / "tests").is_dir():
        return "`tests/` at the repo root"
    return "colocated with the code under test (no top-level `tests/` found)"


def repo_gate_command(repo: Path) -> str | None:
    """The gate-runner invocation `repo` declares, or None when neither is on PATH.

    Args:
        repo: absolute path to the target repo's worktree root.

    Returns:
        `"prek run --files <touched files>"` when `prek` is installed, else `"pre-commit run
        --files <touched files>"` when `pre-commit` is (both share the same
        `.pre-commit-config.yaml`), else None — never invented.
    """
    if shutil.which("prek"):
        return "prek run --files <touched files>"
    if shutil.which("pre-commit"):
        return "pre-commit run --files <touched files>"
    return None


def repo_test_command(repo: Path) -> str:
    """The test-invocation command `repo` declares, probed from disk/PATH.

    Args:
        repo: absolute path to the target repo's worktree root.

    Returns:
        `"uv run pytest <file> -q"` when `uv` is installed and `repo` carries a `uv.lock`, else
        plain `"pytest <file> -q"`.
    """
    if shutil.which("uv") and (repo / "uv.lock").is_file():
        return "uv run pytest <file> -q"
    return "pytest <file> -q"


def repo_conventions_block(repo: Path) -> str:
    """The fixed half of the worker prompt (card point 3): `repo`'s own conventions, probed from
    disk/PATH so they are the same on every card for this repo.

    Args:
        repo: absolute path to the target repo's worktree root.

    Returns:
        The fixed block, newline-joined.
    """
    lines = [
        "Repo conventions, probed from disk — do not retype these from memory:",
        f"- Python tests: {repo_test_location(repo)}.",
        f"- Run tests: `{repo_test_command(repo)}`",
    ]
    gate = repo_gate_command(repo)
    if gate:
        lines.append(f"- Run gates: `{gate}` — must all pass.")
    lines.append(f"- {_WORKER_INVARIANTS}")
    return "\n".join(lines)


def cited_requirement_rows(card_path: Path) -> list[str]:
    """The table rows of the requirements a card cites without defining them.

    The rows come from the open cards (`0-ideas` to `3-in-progress`) of the same chantier — the
    worker may read no card but its own, so the check criterion has to travel in the prompt.
    `story.md` is not a source: the worker reads only the cards of its chantier, so a row that
    lives only in an unmigrated story table is not added.

    Args:
        card_path: absolute path to the card file, `<chantier>/<column>/<slug>.md`.

    Returns:
        Lines in the order the card cites its ids. An id defined identically (same priority,
        statement and check) in every card gives one verbatim row; an id defined with differing
        rows gives a warning line then each distinct row, in board order. An id defined in no
        open card adds nothing. Empty when the card cannot be read.
    """
    try:
        text = card_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    own = defined_rows(text)
    siblings = [
        c for c in read_open_cards(card_path.parent.parent) if c[0] != card_path.stem
    ]
    rows = requirement_rows(siblings)
    lines: list[str] = []
    for rid in cited_ids(frontmatter(text)):
        if rid in own:
            continue
        # Les lignes iso sont dédupliquées (la première rencontrée est gardée, ordre de la board).
        distinct = list(dict.fromkeys(r for r in rows if r.id == rid))
        if len(distinct) > 1:
            lines.append("  (definitions differ across cards — verify with the user)")
        lines += [f"  {r.row}" for r in distinct]
    return lines


def fidelity_levels(items: list[str]) -> dict[str, str]:
    """The fidelity level of every mockup axis, from the card's `axe=niveau` items.

    Args:
        items: the card's `mockup_fidelity:` elements, e.g. ["layout=strict"].

    Returns:
        One level per axis in `AXES` order; an axis the items do not name is `guided`.

    Raises:
        SystemExit: an item names an axis outside `AXES` or a level outside `LEVELS`.
    """
    levels = dict.fromkeys(AXES, "guided")
    for item in items:
        axis, _, level = (part.strip() for part in item.partition("="))
        if axis not in AXES or level not in LEVELS:
            raise SystemExit(
                f"spawn-workers: mockup_fidelity {item!r} — axis one of {list(AXES)}, "
                f"level one of {list(LEVELS)}"
            )
        levels[axis] = level
    return levels


def prepare_mockup(path: Path) -> Path:
    """The path a worker reads the mockup from: `path`, or the unpacked folder of a `.zip`.

    The folder is the sibling `<zip stem>/`: created from the archive when absent, reused as is
    otherwise. Every entry is checked before anything is written.

    Args:
        path: the card's `mockup:` path, `~` already expanded.

    Returns:
        `path` unchanged unless it is a `.zip` file; then the sibling folder.

    Raises:
        SystemExit: an archive entry would land outside the folder.
    """
    if path.suffix.lower() != ".zip" or not path.is_file():
        return path
    target = path.with_suffix("")
    if target.exists():
        if not target.is_dir():
            raise SystemExit(f"spawn-workers: {target} exists and is not a directory")
        return target
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        root = staging.resolve()
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not (root / name).resolve().is_relative_to(root):
                    raise SystemExit(
                        f"spawn-workers: {path} has an entry outside its folder: {name}"
                    )
            archive.extractall(staging)
        staging.rename(target)
    except zipfile.BadZipFile:
        raise SystemExit(f"spawn-workers: {path} is not a valid zip archive") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


def mockup_lines(mockup: Path, levels: dict[str, str]) -> list[str]:
    """The brief lines for a card that declares a mockup.

    Args:
        mockup: the file or folder the worker reads (`prepare_mockup`'s result).
        levels: the level of each axis (`fidelity_levels`'s result).

    Returns:
        The path, the per-axis levels with their meaning, and — unless every axis is `free` —
        the table the worker hands back.
    """
    lines = [
        f"- Mockup (the rendering spec — read it in full before writing any code): {mockup}",
        "- Mockup fidelity per axis: "
        + ", ".join(f"{axis}={level}" for axis, level in levels.items()),
        "  - strict: reproduce identically; if that cannot be met, say so in the report "
        "instead of deviating silently",
        "  - guided: follow the intent, justify every deviation in the report",
        "  - free: the mockup is only an inspiration",
    ]
    reported = [axis for axis, level in levels.items() if level != "free"]
    if reported:
        lines.append(
            "- Mockup report: hand back a table `mockup element | implementation | status` "
            "(status: done | deviation | missing) covering the strict and guided axes "
            f"({', '.join(reported)}); for a guided axis, add the justified deviation."
        )
    return lines


def generated_card_block(
    card_path: Path,
    repo_slug: str,
    arbitrage: str | None = None,
    cited_rows: list[str] | None = None,
    mockup: Path | None = None,
    levels: dict[str, str] | None = None,
) -> str:
    """The 'généré' half of the worker prompt (card point 3): the card, its repo, and any
    judgment call the worker should resolve rather than ask back.

    Args:
        card_path: absolute path to the card file.
        repo_slug: the target repo's `org/repo` slug.
        arbitrage: a judgment call to surface to the worker, or None.
        cited_rows: indented lines (full rows) of the requirements the card cites but does not
            define, or None.
        mockup: the mockup file or folder the card declares, or None.
        levels: the level of each mockup axis; read only with `mockup`.

    Returns:
        The generated block, newline-joined.
    """
    lines = [
        "Card to work:",
        f"- Card: {card_path}",
        f"- Repo: {repo_slug}",
    ]
    if arbitrage:
        lines.append(f"- Judgment call to resolve: {arbitrage}")
    if cited_rows:
        lines.append(
            "- Requirements cited by the card, defined in other cards (full rows):"
        )
        lines += cited_rows
    if mockup:
        lines += mockup_lines(mockup, levels or fidelity_levels([]))
    return "\n".join(lines)


def build_worker_prompt(
    card_path: Path,
    repo: Path,
    repo_slug: str,
    arbitrage: str | None = None,
    mockup: Path | None = None,
    levels: dict[str, str] | None = None,
) -> str:
    """The full prompt for an `Agent(subagent_type: "card-worker", ...)` call: the fixed
    repo-conventions block, then the generated card block.

    Args:
        card_path: absolute path to the card file.
        repo: absolute path to the target repo's worktree root.
        repo_slug: the target repo's `org/repo` slug.
        arbitrage: a judgment call to surface to the worker, or None.
        mockup: the mockup file or folder the card declares, or None.
        levels: the level of each mockup axis; read only with `mockup`.

    Returns:
        The two blocks, separated by a blank line.
    """
    return (
        f"{repo_conventions_block(repo)}\n\n"
        f"{generated_card_block(card_path, repo_slug, arbitrage, cited_requirement_rows(card_path), mockup, levels)}"
    )


def effective_model(card_model: str, requested: str | None) -> str:
    """The model tier for the Agent call: the caller's own judgment on how briefed the card
    already is, or the card's own `model:` frontmatter when the caller passes none.

    Args:
        card_model: the card's `model:` alias, or "".
        requested: the tier passed to this call (`--model`), or None.

    Returns:
        `requested` when given (truthy), else `card_model` verbatim.
    """
    return requested or card_model


def effective_effort(card_effort: str, requested: str | None) -> str:
    """The effort level for a worker: the call's `--effort`, else the card's own `effort:`.

    Args:
        card_effort: the card's `effort:` value, or "".
        requested: the level passed to this call (`--effort`), or None.

    Returns:
        `requested` when given (truthy), else `card_effort`; "" means nothing is imposed.
    """
    return requested or card_effort


def _extract_valued_flag(argv: list[str], flag: str) -> tuple[str | None, list[str]]:
    """Pull `flag <value>` out of `argv`.

    Args:
        argv: the raw argument list.
        flag: the flag to look for, e.g. `"--model"`.

    Returns:
        `(value, rest)` — `value` is None when `flag` is absent; `rest` is `argv` with the flag
        and its value removed.

    Raises:
        SystemExit: `flag` is present with no following value.
    """
    if flag not in argv:
        return None, argv
    i = argv.index(flag)
    if i + 1 >= len(argv):
        raise SystemExit(f"spawn-workers: {flag} needs a value")
    return argv[i + 1], argv[:i] + argv[i + 2 :]


def build_claude_command(
    name: str, chantier_id: str, slug: str, card_model: str, effort: str = ""
) -> str:
    """The `claude` invocation a worker session boots with, as one shell string.

    Args:
        name: the session display name, `<repo>-<slug>`.
        chantier_id: the four-digit chantier number.
        slug: the card filename without ".md".
        card_model: the card's `model:` alias, or "" — `--model` / `--advisor` are derived
            from it (`model_arg`, `advisor_for`), not passed as-is.
        effort: the effective effort level, or "" — then no `--effort` is passed.

    Returns:
        One POSIX shell string — for a sonnet card with effort `low`:
        claude --name pinky-bootstrap-fix --model "sonnet[1m]" --advisor "opus" --effort low "/pc-kanban:chantier <id> <slug>"
    """
    parts = ["claude", "--name", shlex.quote(name)]
    model = model_arg(card_model)
    if model:
        parts += ["--model", f'"{model}"']
    advisor = advisor_for(card_model)
    if advisor:
        parts += ["--advisor", f'"{advisor}"']
    if effort:
        parts += ["--effort", shlex.quote(effort)]
    parts.append(f'"/pc-kanban:chantier {chantier_id} {slug}"')
    return " ".join(parts)


def build_spawn_command(
    mux: str,
    target: SpawnTarget,
    chantier_id: str,
    slug: str,
    card_model: str,
    effort: str = "",
) -> list[str]:
    """Build the `<mux> new-workspace` argv for one repo of a card.

    Args:
        mux: the multiplexer binary, 'cmux' or 'wmux'.
        target: the repo slug and its resolved local path.
        chantier_id: the four-digit chantier number.
        slug: the card filename without ".md".
        card_model: the card's `model:` alias.
        effort: the effective effort level, or "".

    Returns:
        The full argv list, ready for subprocess.run without a shell. The workspace name is
        left to the multiplexer, which adopts the `claude --name` value.
    """
    name = f"{target.slug.split('/')[-1]}-{slug}"
    inner = build_claude_command(name, chantier_id, slug, card_model, effort)
    return [mux, "new-workspace", "--cwd", str(target.path), "--command", inner]


def build_manual_line(
    target: SpawnTarget, chantier_id: str, slug: str, card_model: str, effort: str = ""
) -> str:
    """The `cd <path> && claude ...` line a user runs when no multiplexer is available.

    Args:
        target: the repo slug and its resolved local path.
        chantier_id: the four-digit chantier number.
        slug: the card filename without ".md".
        card_model: the card's `model:` alias.
        effort: the effective effort level, or "".

    Returns:
        One shell line — the repo path (shell-quoted), then `&&`, then the build_claude_command
        string.
    """
    name = f"{target.slug.split('/')[-1]}-{slug}"
    claude = build_claude_command(name, chantier_id, slug, card_model, effort)
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
    """Parse args, resolve the card, then either print each repo's worker prompt (default) or
    spawn/print a `--cmux` command per repo.

    Args:
        argv: command-line arguments without the program name.

    Returns:
        Process exit code.

    Raises:
        SystemExit: the id/slug pair is malformed, the card is missing or ambiguous, a repo
            slug is not one the card declares, resolve_repos.sh failed, the card declares no
            repos at all, or `--model`/`--effort`/`--arbitrage` is present with no
            value, or an effort is not one of `EFFORTS`.
    """
    dry_run = "--dry-run" in argv
    use_cmux = "--cmux" in argv
    argv = [a for a in argv if a not in ("--dry-run", "--cmux")]
    model_override, argv = _extract_valued_flag(argv, "--model")
    effort_override, argv = _extract_valued_flag(argv, "--effort")
    arbitrage, argv = _extract_valued_flag(argv, "--arbitrage")
    positional = argv
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
    if model_override and model_override not in _MODEL_ARG:
        raise SystemExit(
            f"spawn-workers: --model {model_override!r} — not one of {sorted(_MODEL_ARG)}"
        )

    if effort_override and effort_override not in EFFORTS:
        raise SystemExit(
            f"spawn-workers: --effort {effort_override!r} — not one of {list(EFFORTS)}"
        )
    card_effort = _hook("--card-effort", str(card_path), allow_empty=True)
    if card_effort and card_effort not in EFFORTS:
        raise SystemExit(
            f"spawn-workers: card '{slug}' has effort: {card_effort!r} — "
            f"not one of {list(EFFORTS)}"
        )
    effort = effective_effort(card_effort, effort_override)

    slugs = select_repos(declared, wanted)
    targets = resolve_paths(board_workspace_root(card_path), slugs)

    if use_cmux:
        mux = multiplexer()
        if mux is None:
            print(
                f"# no cmux/wmux here — open a worker session yourself in each of the "
                f"{len(targets)} repo(s):"
            )
            for target in targets:
                if not dry_run:
                    _report_enabled(target, enable_project_plugins(target.path))
                print(build_manual_line(target, chantier_id, slug, card_model, effort))
            return 0

        for target in targets:
            if not dry_run:
                _report_enabled(target, enable_project_plugins(target.path))
            command = build_spawn_command(
                mux, target, chantier_id, slug, card_model, effort
            )
            if dry_run:
                print(shlex.join(command))
                continue
            subprocess.run(command, check=True)
            print(f"spawned {target.slug.split('/')[-1]}-{slug} → {target.path}")
        return 0

    # Default: print each repo's worker prompt + resolved tier. The caller passes them to
    # `Agent(subagent_type: "card-worker", model: <tier>, effort: <level>, prompt: <text>)`
    # (`effort` only when printed); this script never calls the Agent tool itself.
    tier = effective_model(card_model, model_override)
    mockup: Path | None = None
    levels = None
    declared_mockup = _hook("--card-mockup", str(card_path), allow_empty=True)
    if declared_mockup:
        mockup = Path(declared_mockup).expanduser()
        if not mockup.exists():
            raise SystemExit(
                f"spawn-workers: card '{slug}' has mockup: {mockup} — not found"
            )
        levels = fidelity_levels(
            _hook(
                "--card-mockup-fidelity", str(card_path), allow_empty=True
            ).splitlines()
        )
        mockup = prepare_mockup(mockup)
    for target in targets:
        prompt = build_worker_prompt(
            card_path, target.path, target.slug, arbitrage, mockup, levels
        )
        name = f"{target.slug.split('/')[-1]}-{slug}"
        print(f"=== worker: {name} ===")
        print(f"model: {tier}")
        if effort:
            print(f"effort: {effort}")
        print("--- prompt ---")
        print(prompt)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
