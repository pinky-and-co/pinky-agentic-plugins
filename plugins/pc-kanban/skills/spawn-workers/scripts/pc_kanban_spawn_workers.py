#!/usr/bin/env python3
"""pc_kanban_spawn_workers.py — hand one kanban card, per repo, to a `card-worker` agent.

The orchestrator session (opened outside any git repo, board curation only) runs this to resolve a
selected card — its `repos:`, its `model:`, and each repo's local clone — then, for each target
repo, prints the two-block prompt (`build_worker_prompt`) and the resolved model tier
(`effective_model`) the orchestrator hands to an `Agent(subagent_type: "card-worker", ...)` call.
This script never calls that tool itself — only the calling session can — so its job stops at
computing the deterministic half: the repo's own conventions (test location, gates/test commands,
probed from disk/PATH rather than retyped from memory — the source of the 2026-09-20 witness run's
miss) plus the card's absolute path, its repo, and any judgment call to resolve.

**`--cmux`** keeps the older path for a repo whose environment does not reduce to commands (a
project-scoped plugin resolved at process start, a scoped MCP server) — a real `claude` process is
opened instead of a subagent, one worker session per repo:

- **Inside cmux / wmux** (`CMUX_SURFACE_ID` / `WMUX_SURFACE_ID`): `<mux> new-workspace --cwd <path>
  --command '<claude ...>'`, one per repo, booting `/pc-kanban:chantier <id> <slug>` on the card's
  `model:` (`--advisor` one tier above, never for `opus`/`fable`).
- **Outside any multiplexer**: prints a `cd <path> && claude ...` line per repo for the user to run,
  and spawns nothing.

Only in `--cmux` mode does `enable_project_plugins` run first — at project scope, in the target
repo's *versioned* `.claude/settings.json` — every installed project-scoped plugin whose
`projectPath` covers the repo, so that real session's boot has them live with no `/reload-plugins`.
A `card-worker` subagent carries no such cold-start dependency (it shells the repo's own gates/test
commands directly, no plugin skill in the loop), so the default path skips this entirely.

No dependency on the `pinky` CLI: card lookup, board resolution, `repos:` and `model:`
all go through `stop_handoff.py`'s CLI (same plugin, same pattern guard.py uses); slug->path
resolution reuses `resolve_repos.sh` (sibling skill); the project-scope enable shells out to
`claude plugin` directly. The workspace root is derived from the board that holds the card — never
a configured value.

Multi-repo cards are sequential by convention: pass the single next `<repo-slug>` to
spawn just that one. With no slug filter every repo of the card is spawned, in `repos:` order.

`--model <tier>` overrides the card's own `model:` frontmatter for the Agent call — the caller's
judgment on how much the card is already briefed (a card fully briefed by this skill's own fixed
block needs less than its `model:` says an unbriefed run would). `--arbitrage <text>` carries a
judgment call the generated block should surface to the worker. Neither adds a card frontmatter
field — both are call-time only.

`--dry-run` only affects `--cmux` mode: it previews the command line(s) and skips the project-scope
plugin enable. The default (agent) path already does nothing but print — there is nothing to defer.

Python 3.11+, stdlib only.

Usage: pc_kanban_spawn_workers.py [--dry-run] [--cmux] [--model <tier>] [--arbitrage <text>]
                                   <chantier-id> <card-slug> [<repo-slug>...]
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pc_kanban_spawn_plugin_scope_lib import matching_plugin_ids

_USAGE = (
    "Usage: pc_kanban_spawn_workers.py [--dry-run] [--cmux] [--model <tier>] "
    "[--arbitrage <text>] <chantier-id> <card-slug> [<repo-slug>...]"
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
    """The 'fixe' half of the worker prompt (card point 3): `repo`'s own conventions, probed from
    disk/PATH so they are the same on every card for this repo — the 2026-09-20 witness run's
    hand-typed prompt missed the test location for exactly the opposite reason.

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


def generated_card_block(
    card_path: Path, repo_slug: str, arbitrage: str | None = None
) -> str:
    """The 'généré' half of the worker prompt (card point 3): the card, its repo, and any
    judgment call the worker should resolve rather than ask back.

    Args:
        card_path: absolute path to the card file.
        repo_slug: the target repo's `org/repo` slug.
        arbitrage: a judgment call to surface to the worker, or None.

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
    return "\n".join(lines)


def build_worker_prompt(
    card_path: Path, repo: Path, repo_slug: str, arbitrage: str | None = None
) -> str:
    """The full prompt for an `Agent(subagent_type: "card-worker", ...)` call: the fixed
    repo-conventions block, then the generated card block.

    Args:
        card_path: absolute path to the card file.
        repo: absolute path to the target repo's worktree root.
        repo_slug: the target repo's `org/repo` slug.
        arbitrage: a judgment call to surface to the worker, or None.

    Returns:
        The two blocks, separated by a blank line.
    """
    return (
        f"{repo_conventions_block(repo)}\n\n"
        f"{generated_card_block(card_path, repo_slug, arbitrage)}"
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
    """Parse args, resolve the card, then either print each repo's worker prompt (default) or
    spawn/print a `--cmux` command per repo.

    Args:
        argv: command-line arguments without the program name.

    Returns:
        Process exit code.

    Raises:
        SystemExit: the id/slug pair is malformed, the card is missing or ambiguous, a repo
            slug is not one the card declares, resolve_repos.sh failed, the card declares no
            repos at all, or `--model`/`--arbitrage` is present with no value.
    """
    dry_run = "--dry-run" in argv
    use_cmux = "--cmux" in argv
    argv = [a for a in argv if a not in ("--dry-run", "--cmux")]
    model_override, argv = _extract_valued_flag(argv, "--model")
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

    # Default: print each repo's worker prompt + resolved tier. The caller invokes
    # `Agent(subagent_type: "card-worker", model: <tier>, prompt: <printed text>)` itself — this
    # script never calls the Agent tool, only the session running it can.
    tier = effective_model(card_model, model_override)
    for target in targets:
        prompt = build_worker_prompt(card_path, target.path, target.slug, arbitrage)
        name = f"{target.slug.split('/')[-1]}-{slug}"
        print(f"=== worker: {name} ===")
        print(f"model: {tier}")
        print("--- prompt ---")
        print(prompt)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
