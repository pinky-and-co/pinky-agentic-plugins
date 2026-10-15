#!/usr/bin/env python3
"""Measure the `freeze` Bash guard: replay real transcripts, or diff two versions of `guard.py`.

Two subcommands, neither of which changes the guard:

- `transcripts` counts, per command form, the unique Bash commands of the Claude Code
  transcripts, main sessions and subagents apart.
- `diff --base <git-ref>` runs a generated grid of commands through `guard.py` at `<git-ref>`
  and through the working tree, and lists each verdict that changed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import types
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

GUARD_PATH = Path(__file__).resolve().with_name("guard.py")
EXAMPLE_WIDTH = 80

_ARG = r"""(?:"[^"\n]*"|'[^'\n]*'|[^\s;|&"'<>]+)"""
_UNQUOTED = r"""[^\s;|&"'<>]+"""


@dataclass(frozen=True)
class Form:
    """A command shape: its pattern and the group holding the written target, if any."""

    pattern: re.Pattern[str]
    group: int | None


# Order is the report order.
FORMS: dict[str, Form] = {
    "cd-quoted": Form(
        re.compile(r"""\b(?:cd|pushd)\s+(["'])(?![^"'\n]*\$)(.*?)\1"""), 2
    ),
    "git-C-quoted": Form(
        re.compile(r"""\bgit\s+-C\s+(["'])(?![^"'\n]*\$)(.*?)\1"""),
        2,
    ),
    "quoted-var-target": Form(
        re.compile(r"""\b(?:cd|pushd|git\s+-C)\s+(["'])([^"'\n]*\$[^"'\n]*)\1"""), 2
    ),
    "dd-of": Form(re.compile(r"\bdd\b[^\n;|&]*?\bof=(" + _UNQUOTED + ")"), 1),
    "curl-o": Form(
        re.compile(r"\bcurl\b[^\n;|&]*?\s(?:-o|--output)[\s=]*(" + _ARG + ")"), 1
    ),
    "wget-O": Form(
        re.compile(r"\bwget\b[^\n;|&]*?\s(?:-O|--output-document)[\s=]*(" + _ARG + ")"),
        1,
    ),
    "tar-C": Form(re.compile(r"\btar\b[^\n;|&]*?\s-C\s*(" + _ARG + ")"), 1),
    "unzip-d": Form(re.compile(r"\bunzip\b[^\n;|&]*?\s-d\s*(" + _ARG + ")"), 1),
    "git-clone-dir": Form(
        re.compile(r"\bgit\s+clone\s+(?:-\S+\s+)*\S+\s+(" + _UNQUOTED + ")"), 1
    ),
    "pipe-ampersand": Form(re.compile(r"\|&"), None),
    "system-path-dotdot": Form(
        re.compile(
            r"(/(?:tmp|var|dev|usr|etc|opt|private)/[^\s;|&\"']*\.\./[^\s;|&\"']*)"
        ),
        1,
    ),
}


def load_guard(source: str, label: str) -> types.ModuleType:
    """Execute `source` as the `guard` module, located where the real one is.

    Args:
        source: The text of a `guard.py`.
        label: A name for the module, distinct per version loaded.

    Returns:
        The module, `__file__` set to the real `guard.py` so its plugin-relative imports resolve.
    """
    module = types.ModuleType(label)
    module.__file__ = str(GUARD_PATH)
    exec(compile(source, str(GUARD_PATH), "exec"), module.__dict__)  # nosec B102 - the repo's own guard.py at a git ref
    return module


def load_tree_guard(label: str = "guard_tree") -> types.ModuleType:
    """Load the working-tree `guard.py`.

    Args:
        label: A name for the module.

    Returns:
        The guard module.
    """
    return load_guard(GUARD_PATH.read_text(encoding="utf-8"), label)


def load_ref_guard(ref: str) -> types.ModuleType:
    """Load `guard.py` as of a git ref.

    Args:
        ref: Any git revision.

    Returns:
        The guard module at that revision.

    Raises:
        SystemExit: When git cannot show the file at that ref.
    """
    top = subprocess.run(
        ["git", "-C", str(GUARD_PATH.parent), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    rel = os.path.relpath(GUARD_PATH, Path(top).resolve())
    shown = subprocess.run(
        ["git", "-C", top, "show", f"{ref}:{rel}"], capture_output=True, text=True
    )
    if shown.returncode != 0:
        raise SystemExit(f"git show {ref}:{rel} failed: {shown.stderr.strip()}")
    return load_guard(shown.stdout, "guard_base")


def one_line(command: str, width: int = EXAMPLE_WIDTH) -> str:
    """Fold a command onto one line of at most `width` characters.

    Args:
        command: Any command, possibly multiline.
        width: The maximum length of the result.

    Returns:
        The command with whitespace runs collapsed, cut with an ellipsis when too long.
    """
    flat = " ".join(command.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def iter_bash_commands(path: Path) -> Iterator[tuple[str, str]]:
    """Yield the `(command, cwd)` of each Bash tool call of a transcript.

    Args:
        path: A `.jsonl` transcript.

    Yields:
        One pair per Bash tool call; unreadable lines are skipped.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        if '"Bash"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == "Bash"
                and isinstance((block.get("input") or {}).get("command"), str)
            ):
                yield block["input"]["command"], event.get("cwd") or ""


def _outside(target: str, cwd: str) -> bool:
    """Return whether `target`, resolved against `cwd`, leaves `cwd`.

    Args:
        target: A path as written, possibly relative, `~`-prefixed or holding a `$variable`.
        cwd: The directory the command ran in.

    Returns:
        False for an empty `cwd` or a target holding `$` (unresolvable).
    """
    target = target.strip("\"'")
    if not cwd or "$" in target:
        return False
    resolved = Path(os.path.normpath(os.path.join(cwd, os.path.expanduser(target))))
    base = Path(os.path.normpath(cwd))
    return resolved != base and base not in resolved.parents


def replay_transcripts(root: Path, guard: types.ModuleType) -> dict:
    """Count the command forms of every transcript under `root`.

    Args:
        root: The folder holding the `.jsonl` transcripts.
        guard: The guard module to ask about each command.

    Returns:
        A dict keyed by scope (`main`, `subagent`), each holding per form the keys `total`,
        `outside`, `asked`, `examples`; the key `unique` gives the distinct commands per scope.
    """
    seen: dict[str, set[tuple[str, str]]] = {"main": set(), "subagent": set()}
    for path in sorted(root.rglob("*.jsonl")):
        scope = "subagent" if "/subagents/" in path.as_posix() else "main"
        seen[scope].update(iter_bash_commands(path))
    result: dict = {"unique": {s: len(v) for s, v in seen.items()}}
    for scope, pairs in seen.items():
        forms: dict[str, dict] = defaultdict(
            lambda: {"total": 0, "outside": 0, "asked": 0, "examples": []}
        )
        for command, cwd in sorted(pairs):
            for form, shape in FORMS.items():
                group = shape.group
                matches = list(shape.pattern.finditer(command))
                if not matches:
                    continue
                row = forms[form]
                row["total"] += 1
                if group is not None and any(
                    _outside(m.group(group), cwd) for m in matches
                ):
                    row["outside"] += 1
                if cwd and guard.bash_write_escapes(command, [cwd], Path(cwd)):
                    row["asked"] += 1
                row["examples"].append(command)
        result[scope] = {form: forms[form] for form in FORMS if form in forms}
    return result


def format_report(result: dict, examples: int) -> str:
    """Render a replay result.

    Args:
        result: The output of `replay_transcripts`.
        examples: At most this many examples per form, each on one line.

    Returns:
        The report text.
    """
    out: list[str] = []
    for scope in ("main", "subagent"):
        out.append(f"== {scope}: {result['unique'][scope]} unique commands")
        out.append(f"{'form':<22}{'total':>7}{'outside':>9}{'asked':>7}")
        for form in FORMS:
            row = result[scope].get(
                form, {"total": 0, "outside": 0, "asked": 0, "examples": []}
            )
            out.append(
                f"{form:<22}{row['total']:>7}{row['outside']:>9}{row['asked']:>7}"
            )
            out.extend(f"    {one_line(c)}" for c in row["examples"][:examples])
    return "\n".join(out)


def _grid(root: Path) -> list[tuple[str, str, str]]:
    """Generate the `(family, label, command)` grid of the differential test.

    Args:
        root: The frozen scope; commands are meant to run with it as cwd.

    Returns:
        The commands, tagged by family.
    """
    inside = "sub/dir"
    outside = "../outside"
    tails = ["echo x > f", "echo x | tee f", "git commit -am x"]
    targets: list[tuple[str, str, str]] = []
    for where, rel in (("in", inside), ("out", outside)):
        targets += [
            ("bare-target", f"bare-{where}", rel),
            ("quoted-target", f"single-{where}", f"'{rel}'"),
            ("quoted-target", f"double-{where}", f'"{rel}"'),
            (
                "quoted-target",
                f"space-{where}",
                f'"{rel.replace("dir", "di r").replace("outside", "out side")}"',
            ),
            ("quoted-var-target", f"var-{where}", f'"$REPO/{rel}"'),
            ("quoted-var-target", f"var-single-{where}", f"'${{REPO}}/{rel}'"),
        ]
    grid: list[tuple[str, str, str]] = []
    for family, label, target in targets:
        for tail in tails:
            grid.append((family, f"cd {label} {tail}", f"cd {target} && {tail}"))
            grid.append((family, f"git-C {label} {tail}", f"git -C {target} {tail}"))
    abs_in, abs_out = f"{root}/a.txt", "/home/elsewhere/a.txt"
    for where, path in (("in", abs_in), ("out", abs_out)):
        grid += [
            ("write-redirect", f"redirect {where}", f"echo x > {path}"),
            ("write-redirect", f"append {where}", f"echo x >> {path}"),
            ("write-tee", f"tee {where}", f"echo x | tee {path}"),
            ("write-commit", f"commit {where}", f"git -C {path} commit -am x"),
        ]
    for n in (1, 50, 400):
        grid.append(("long-git-segment", f"git x{n}", "git " * n + "notes"))
        grid.append(
            ("long-git-segment", f"git x{n} commit", "git " * n + "commit -m x")
        )
    return grid


def diff_guards(base: types.ModuleType, head: types.ModuleType) -> list[dict]:
    """Run the grid through two guards and list the verdicts that differ.

    Args:
        base: The reference guard.
        head: The guard under test.

    Returns:
        One dict per changed verdict: family, label, command, before, after.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        changed = []
        for family, label, command in _grid(root):
            before = bool(base.bash_write_escapes(command, [str(root)], root))
            after = bool(head.bash_write_escapes(command, [str(root)], root))
            if before != after:
                changed.append(
                    {
                        "family": family,
                        "label": label,
                        "command": one_line(command),
                        "before": before,
                        "after": after,
                    }
                )
    return changed


def _cmd_transcripts(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser()
    print(format_report(replay_transcripts(root, load_tree_guard()), args.examples))
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    changed = diff_guards(load_ref_guard(args.base), load_tree_guard())
    allowed = set(args.allow_family)
    for item in changed:
        verdict = f"{'ask' if item['before'] else 'pass'} -> {'ask' if item['after'] else 'pass'}"
        print(f"[{item['family']}] {item['label']}: {verdict}  {item['command']}")
    stray = [c for c in changed if c["family"] not in allowed]
    print(
        f"{len(changed)} verdict(s) changed, {len(stray)} outside the allowed families"
    )
    return 1 if stray else 0


def main(argv: list[str] | None = None) -> int:
    """Run the command line.

    Args:
        argv: Arguments, `sys.argv[1:]` by default.

    Returns:
        The exit code; 1 when `diff` finds a change outside `--allow-family`.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    tr = sub.add_parser("transcripts", help="count command forms in transcripts")
    tr.add_argument("--root", default="~/.claude/projects")
    tr.add_argument("--examples", type=int, default=0)
    tr.set_defaults(func=_cmd_transcripts)
    df = sub.add_parser("diff", help="compare verdicts against a git ref")
    df.add_argument("--base", required=True)
    df.add_argument("--allow-family", action="append", default=[])
    df.set_defaults(func=_cmd_diff)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
