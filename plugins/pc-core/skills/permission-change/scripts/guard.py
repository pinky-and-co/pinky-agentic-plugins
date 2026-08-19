#!/usr/bin/env python3
"""PreToolUse guard — /permission-change careful|freeze|guard|deny.

A slash command in Claude Code is only a prompt: it cannot enforce anything on its own. So the
guard rails are split in two — a tiny **state file** the `permission-change` skill toggles, and
this **single script** that reads it. The script is dual-mode:

- **Hook mode** (no argv, PreToolUse JSON on stdin): enforces the active state.
  - **Generated-tree guard, ALWAYS ON** → an `Edit`/`Write`/`MultiEdit`/`NotebookEdit` targeting a
    known generated tree (`.claude/**`, `docs/reference/generated/**`) returns
    ``permissionDecision: "deny"`` unconditionally — no opt-in, unlike the two below. A generated
    tree survives a direct edit only until the next regeneration, with no error at either end —
    this guard makes that loss mode loud instead of silent. Layered ABOVE `freeze` — a freeze
    scoped to `.claude` must not reopen what this guard closes.
  - **Outside-scope guard, ALWAYS ON** → an `Edit`/`Write`/`MultiEdit`/`NotebookEdit` whose target
    sits outside the project root, every `--add-dir`/`/add-dir` directory (read live from the
    event's `workspace.added_dirs` field — a common field the harness fills on every hook call,
    no state file needed), and every declared `permissions.additionalDirectories` entry returns
    ``permissionDecision: "deny"``. Read-visibility (Claude Code natively loads ancestor
    `CLAUDE.md` files as context) is not write authorization — this guard makes that boundary
    mechanical instead of relying on the agent to remember it.
  - `careful` on  → a destructive `Bash` command returns ``permissionDecision: "ask"`` (interactive
    confirm; the user clears it with one keystroke). Never a silent denial — careful is a speed bump.
  - `freeze`  set → an `Edit`/`Write`/`MultiEdit`/`NotebookEdit` whose target sits **outside** the
    frozen directories returns ``permissionDecision: "deny"`` (a lock, not a prompt). HONEST LIMIT:
    the lock covers the write-class tools only — a Bash write (`sed -i`, `tee`, `>`) is not
    intercepted, and careful's destructive catalogue does not cover those either. No shell-parsing
    whack-a-mole here; a hard path guarantee belongs to ``permissions.deny``. The always-on guards
    above are the place a hard guarantee IS worth the code: `permissions.deny` entries cannot carry
    a message, and the point here is the guidance text, not the block itself.
  - `deny`  set → an `Edit`/`Write`/`MultiEdit`/`NotebookEdit` whose target sits **inside** any
    denied directory returns ``permissionDecision: "deny"``. The inverse of `freeze`: a blocklist,
    not a whitelist — everything outside the denied directories stays writable.
  - Anything else → no output, exit 0 → normal permission flow (we never emit ``allow``, which would
    short-circuit other hooks).

- **Ctl mode** (argv): the `permission-change` skill calls
  ``guard.py careful|freeze|guard|deny [args]`` or ``guard.py status`` to mutate the state file
  deterministically (no LLM guesswork about JSON shape) — one skill forwarding `$ARGUMENTS`
  straight through, instead of four near-identical skills each hardcoding their own mode.

State lives at ``$CLAUDE_PROJECT_DIR/.claude/.guard-state.json`` (override: ``PC_GUARD_STATE``).
It persists across turns until explicitly lifted; every deny/ask reason says how to lift it.

Python 3.11+, stdlib only. A hook failure must never block a tool call → unexpected errors exit 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# Windows' default console codepage (cp1252, not UTF-8) can't encode this hook's ✓/⚠ output and
# crashes with UnicodeEncodeError. Reconfigure standalone, since this script runs as a subprocess
# and cannot rely on a caller having already fixed the stream encoding.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

STATE_ENV = "PC_GUARD_STATE"
DEFAULT_STATE_REL = ".claude/.guard-state.json"

# Tools whose target path `freeze` locks to the frozen scope. Bash is handled by `careful`, not here.
FREEZE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Curated destructive-command catalogue (label, pattern). Labels surface in the confirm prompt.
# Tunable on purpose — kept readable so a profile author can extend it for their stack.
_DESTRUCTIVE: list[tuple[str, re.Pattern[str]]] = [
    ("rm -rf", re.compile(r"\brm\s+(?:-\w*[rf]\w*\s+)+", re.I)),
    (
        "git push --force",
        re.compile(
            r"\bgit\s+push\b[^\n]*(?:--force(?:-with-lease)?|\s-f\b|\s\+\w)", re.I
        ),
    ),
    ("git reset --hard", re.compile(r"\bgit\s+reset\b[^\n]*--hard\b", re.I)),
    ("git clean", re.compile(r"\bgit\s+clean\b[^\n]*-\w*[fd]", re.I)),
    (
        "git checkout (discard)",
        re.compile(r"\bgit\s+checkout\b[^\n]*(?:--\s|\s\.\s*$|\s\.\s)", re.I),
    ),
    ("git branch -D", re.compile(r"\bgit\s+branch\b[^\n]*-D\b")),
    (
        "DROP <object>",
        re.compile(
            r"\bDROP\s+(?:TABLE|DATABASE|SCHEMA|VIEW|STAGE|WAREHOUSE|ROLE|USER|FUNCTION|PROCEDURE|STREAM|TASK)\b",
            re.I,
        ),
    ),
    ("TRUNCATE", re.compile(r"\bTRUNCATE\s+(?:TABLE\b)?", re.I)),
    (
        "DELETE FROM (no WHERE)",
        re.compile(r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)", re.I | re.S),
    ),
    ("dd", re.compile(r"\bdd\s+if=", re.I)),
    ("mkfs", re.compile(r"\bmkfs\b", re.I)),
    ("shred", re.compile(r"\bshred\b", re.I)),
    ("chmod -R", re.compile(r"\bchmod\s+-R\b", re.I)),
    ("sudo", re.compile(r"\bsudo\b")),
    (
        "git commit --no-verify / bypass signing",
        # \bverify\b alone would false-positive on ordinary commit messages ("fix: verify the
        # thing") — the flag forms only. `-c commit.gpgsign=false` is the config-override escape
        # hatch alongside --no-gpg-sign; git allows either order around -c.
        re.compile(r"--no-verify|--no-gpg-sign|-c\s+commit\.gpgsign=false", re.I),
    ),
]

# Generated trees, ALWAYS ON — no opt-in, unlike careful/freeze above. Two distinct messages
# because the regenerator differs: `.claude/` is a plugin's install target, regenerated by
# reloading the plugin; `docs/reference/generated/` is typically populated by a repo-specific
# build step, which this generic hook cannot name — a wrong specific command would be worse than a
# generic redirect.
GENERATED_TREES: list[tuple[str, str]] = [
    (
        ".claude",
        "This tree is generated by a plugin — edit the plugin's own source, then run "
        "`/reload-plugins --force` to regenerate.",
    ),
    (
        "docs/reference/generated",
        "This is a generated tree — never hand-edit it. Check this repo's own docs for the "
        "command that rebuilds it (a pre-commit hook, a doc-mirror script, or similar).",
    ),
]


# ── state ─────────────────────────────────────────────────────────────────────


def project_root(event: dict | None = None) -> Path:
    """Resolve the project root: $CLAUDE_PROJECT_DIR > event cwd > current dir."""
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        return Path(proj)
    if event and event.get("cwd"):
        return Path(event["cwd"])
    return Path.cwd()


def state_path(root: Path) -> Path:
    """Where the guard state lives — env override, else `<root>/.claude/.guard-state.json`."""
    override = os.environ.get(STATE_ENV)
    return Path(override) if override else root / DEFAULT_STATE_REL


def load_state(path: Path) -> dict:
    """Read the state file; tolerate absent / malformed content (returns the empty default)."""
    empty = {"careful": False, "freeze": [], "deny": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("careful", False)
    for key in ("freeze", "deny"):
        values = data.get(key)
        data[key] = [str(d) for d in values] if isinstance(values, list) else []
    return data


def save_state(path: Path, state: dict) -> None:
    """Persist the state file, stamping the update time. Creates the parent dir if needed."""
    state = {
        "careful": bool(state.get("careful")),
        "freeze": list(state.get("freeze") or []),
        "deny": list(state.get("deny") or []),
    }
    state["updated"] = f"{datetime.now():%Y-%m-%d %H:%M}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# ── enforcement primitives ─────────────────────────────────────────────────────


def is_destructive(command: str) -> str | None:
    """Return the label of the first destructive pattern the command matches, else None."""
    for label, pattern in _DESTRUCTIVE:
        if pattern.search(command):
            return label
    return None


def target_path(tool_name: str, tool_input: dict) -> str | None:
    """Extract the file a write-class tool targets (NotebookEdit uses `notebook_path`)."""
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path")
    return tool_input.get("file_path")


def generated_tree_violation(target: str, root: Path) -> str | None:
    """Return the regeneration guidance when `target` sits inside a generated tree, else None.

    Relative-to-root match only, deliberately: a generated tree is a workspace-relative concept
    (`.claude/` at THIS repo's root), so a `.claude/` inside some unrelated tree elsewhere on disk
    (a vendored checkout, a different repo) is not a match — never a bare basename scan, which would
    over-block. Uses `is_relative_to` (a real path-segment check), not a string prefix, so
    `.claude-like/x.md` or `docs/reference/explanation.md` cannot collide with `.claude/x.md` or
    `docs/reference/generated/x.md` respectively.
    """
    tgt = Path(target)
    tgt = (tgt if tgt.is_absolute() else root / tgt).resolve()
    try:
        rel = tgt.relative_to(root.resolve())
    except ValueError:
        return None  # outside the workspace root entirely — the outside-scope guard's concern
    for prefix, message in GENERATED_TREES:
        prefix_path = Path(prefix)
        if rel == prefix_path or rel.is_relative_to(prefix_path):
            return message
    return None


def additional_dirs(event: dict, root: Path) -> list[Path]:
    """Directories this session may write to beyond the project root.

    Two sources, merged: `workspace.added_dirs` on the PreToolUse event itself — a common field
    the harness fills on every hook call with the live `--add-dir` / `/add-dir` list, no state
    file needed — and `permissions.additionalDirectories` in every settings file the harness reads
    (project, project-local, user-global), for directories granted that way instead.
    """
    dirs: list[Path] = []
    for d in (event.get("workspace") or {}).get("added_dirs") or []:
        dp = Path(str(d))
        dirs.append(dp if dp.is_absolute() else root / dp)
    for f in (
        root / ".claude" / "settings.json",
        root / ".claude" / "settings.local.json",
        Path.home() / ".claude" / "settings.json",
    ):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for d in (data.get("permissions") or {}).get("additionalDirectories") or []:
            dp = Path(str(d))
            dirs.append(dp if dp.is_absolute() else root / dp)
    return dirs


def outside_scope_violation(event: dict, target: str, root: Path) -> str | None:
    """Return guidance when `target` sits outside the project root AND every declared
    additional directory, else None. ALWAYS ON, no opt-out — like the generated-tree guard above.

    Read-visibility is not write authorization: Claude Code natively loads ancestor `CLAUDE.md`
    files as context (walking up from the project root), and that read access says nothing about
    whether a write to that same ancestor file was ever asked for.
    """
    tgt = Path(target)
    tgt = (tgt if tgt.is_absolute() else root / tgt).resolve()
    allowed = [root.resolve(), *(d.resolve() for d in additional_dirs(event, root))]
    if any(tgt == a or tgt.is_relative_to(a) for a in allowed):
        return None
    return (
        f"`{target}` resolves outside the project root (`{root}`) and every `--add-dir` / "
        "`permissions.additionalDirectories` directory. A file Claude Code loads as ancestor "
        "context (e.g. a CLAUDE.md one level up) is read-visible by design — that is not write "
        "authorization. If this edit is genuinely intended, do it by hand, or add the directory "
        "with `/add-dir` or `permissions.additionalDirectories` first."
    )


def frozen_violation(target: str, freeze_dirs: list[str], root: Path) -> str | None:
    """Return the frozen scope a target escapes, or None when it is inside one (or no freeze).

    `freeze_dirs` are the ONLY writable zones while a freeze is active: a target is allowed iff it
    sits inside at least one of them. Relative paths resolve against the project root.
    """
    if not freeze_dirs:
        return None
    tgt = Path(target)
    tgt = (tgt if tgt.is_absolute() else root / tgt).resolve()
    for d in freeze_dirs:
        dp = Path(d)
        dp = (dp if dp.is_absolute() else root / dp).resolve()
        if tgt == dp or tgt.is_relative_to(dp):
            return None
    return freeze_dirs[0] if len(freeze_dirs) == 1 else ", ".join(freeze_dirs)


def denied_violation(target: str, deny_dirs: list[str], root: Path) -> str | None:
    """Return the denied scope a target falls inside, or None when it matches none (or no deny).

    The inverse of `frozen_violation`: `deny_dirs` are the ONLY forbidden zones while a deny list
    is active — everything else stays writable. Relative paths resolve against the project root.
    """
    if not deny_dirs:
        return None
    tgt = Path(target)
    tgt = (tgt if tgt.is_absolute() else root / tgt).resolve()
    for d in deny_dirs:
        dp = Path(d)
        dp = (dp if dp.is_absolute() else root / dp).resolve()
        if tgt == dp or tgt.is_relative_to(dp):
            return d
    return None


def _decision(kind: str, reason: str) -> dict:
    """Wrap a PreToolUse permission decision in the harness-expected envelope."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": kind,
            "permissionDecisionReason": reason,
        }
    }


def decide(event: dict, root: Path) -> dict | None:
    """Core hook decision: ask on careful, deny on freeze escape, else None (let flow proceed)."""
    state = load_state(state_path(root))
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}

    if tool == "Bash" and state.get("careful"):
        label = is_destructive(tool_input.get("command", ""))
        if label:
            return _decision(
                "ask",
                f"careful: '{label}' is a destructive command — confirm before it runs. "
                f"Lift the guard with `/permission-change careful off`.",
            )

    if tool in FREEZE_TOOLS:
        target = target_path(tool, tool_input)
        if target:
            # Standing invariants first — neither is session-scoped, so neither must be reachable
            # through a freeze whose own scope happens to include what they close.
            message = generated_tree_violation(target, root)
            if message:
                return _decision("deny", message)

            message = outside_scope_violation(event, target, root)
            if message:
                return _decision("deny", message)

            if state.get("deny"):
                scope = denied_violation(target, state["deny"], root)
                if scope:
                    return _decision(
                        "deny",
                        f"deny: `{scope}` is on the deny list; `{target}` falls inside it. "
                        f"Lift it with `/permission-change deny off`.",
                    )

            if state.get("freeze"):
                scope = frozen_violation(target, state["freeze"], root)
                if scope:
                    return _decision(
                        "deny",
                        f"freeze: edits are locked to `{scope}`; `{target}` is outside it. "
                        f"Lift the lock with `/permission-change freeze off`.",
                    )
    return None


# ── hook mode ───────────────────────────────────────────────────────────────--


def run_hook() -> int:
    """Read the PreToolUse event from stdin, emit a decision when one applies, never raise."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0
    try:
        decision = decide(event, project_root(event))
    except Exception:  # a guard must never block a tool call on its own bug
        return 0
    if decision is not None:
        print(json.dumps(decision))
    return 0


# ── ctl mode (skills) ────────────────────────────────────────────────────────


def _onoff(arg: str | None, default: bool = True) -> bool:
    """Parse an on/off argument; absent → `default`."""
    if arg is None:
        return default
    return arg.strip().lower() not in {"off", "false", "0", "no"}


def format_status(state: dict) -> str:
    """One-line human summary of the active guards."""
    careful = "on" if state.get("careful") else "off"
    freeze = state.get("freeze") or []
    freeze_str = ", ".join(freeze) if freeze else "off"
    deny = state.get("deny") or []
    deny_str = ", ".join(deny) if deny else "off"
    return f"careful: {careful} · freeze: {freeze_str} · deny: {deny_str}"


USAGE = (
    "usage: guard.py careful [on|off]\n"
    "       guard.py freeze <dir>... | off\n"
    "       guard.py guard <dir>... | on | off\n"
    "       guard.py deny <dir>... | off\n"
    "       guard.py status"
)


def ctl(argv: list[str], root: Path) -> int:
    """Mutate the state file from a skill invocation; print the resulting status.

    A single `permission-change` skill forwards `$ARGUMENTS` straight through — one positional
    mode word (`careful`/`freeze`/`guard`/`deny`/`status`) picks the branch below, instead of four
    near-duplicate skills each hardcoding their own mode.
    """
    path = state_path(root)
    state = load_state(path)

    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    mode = argv[0]
    rest = argv[1:]

    if mode == "status":
        print(format_status(state))
        return 0
    elif mode == "careful":
        state["careful"] = _onoff(rest[0] if rest else None)
    elif mode == "freeze":
        if rest and rest[0].lower() in {"off", "none", "clear"}:
            state["freeze"] = []
        elif rest:
            state["freeze"] = rest
        else:
            print("usage: guard.py freeze <dir>... | off", file=sys.stderr)
            return 2
    elif mode == "deny":
        if rest and rest[0].lower() in {"off", "none", "clear"}:
            state["deny"] = []
        elif rest:
            state["deny"] = rest
        else:
            print("usage: guard.py deny <dir>... | off", file=sys.stderr)
            return 2
    elif mode == "guard":
        on = _onoff(rest[0] if rest else None)
        if not on:
            state["careful"], state["freeze"] = False, []
        else:
            state["careful"] = True
            dirs = [a for a in rest if a.lower() not in {"on", "true", "1", "yes"}]
            if dirs:
                state["freeze"] = dirs
    else:
        print(f"unknown mode: {mode}\n{USAGE}", file=sys.stderr)
        return 2

    save_state(path, state)
    print(format_status(state))
    return 0


def main(argv: list[str]) -> int:
    """Dispatch: argv present → ctl mode; otherwise → hook mode (stdin)."""
    if argv:
        return ctl(argv, project_root())
    return run_hook()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
