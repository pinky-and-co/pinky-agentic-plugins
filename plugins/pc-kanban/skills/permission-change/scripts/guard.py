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
    sits outside the project root, every directory a `/add-dir` typed in THIS session added (the
    `DirectoryAdded` hook files it in the session's state), and every declared
    `permissions.additionalDirectories` entry (`.claude/settings.json`,
    `.claude/settings.local.json`, `~/.claude/settings.json`) returns ``ask``. A `--add-dir` passed
    at startup emits no event and sits in no settings file: the guard cannot see it, so it never
    answers ``deny`` for an unknown target. Read-visibility (Claude Code natively loads ancestor
    `CLAUDE.md` files as context) is not write authorization — this guard makes that boundary
    mechanical instead of relying on the agent to remember it. Two carve-outs:
    - **Board dirs are always writable** — the card and agent-log directories of every kanban
      board mounted on this machine (`stop_handoff.py --list-kanbans` / `--list-agent-log-dirs`),
      whatever the session root. An orchestrator or a repo-scoped worker session must still be
      able to maintain the boards. This carve-out also clears `freeze` (a whitelist lock), but
      not an explicit `deny` — deny-listing a board dir is a deliberate choice and wins.
    - **The session's own scratchpad is always writable** — same freeze/deny treatment as board
      dirs (see `is_session_scratchpad`). The harness's own banner already tells the agent this
      directory needs no permission prompt; without this carve-out a `context: fork` skill that
      writes there (no channel back to answer an ``ask``) stalls waiting on a prompt nobody can
      answer.
  - `careful` on  → a destructive `Bash` command returns ``permissionDecision: "ask"`` (interactive
    confirm; the user clears it with one keystroke). Never a silent denial — careful is a speed bump.
  - `freeze`  set → an `Edit`/`Write`/`MultiEdit`/`NotebookEdit` whose target sits **outside** the
    frozen directories returns ``permissionDecision: "deny"`` (a lock, not a prompt) — but only for
    a target inside the project root or an added directory: the outside-scope guard above runs
    first, so a target further out gets its ``ask``, never this ``deny``. `freeze` does not judge
    `Bash`: `careful` covers destructive commands, and Claude Code's own permissions and sandbox
    cover the rest.
  - `deny`  set → an `Edit`/`Write`/`MultiEdit`/`NotebookEdit` whose target sits **inside** any
    denied directory returns ``permissionDecision: "deny"``. The inverse of `freeze`: a blocklist,
    not a whitelist — everything outside the denied directories stays writable.
  - Anything else → no output, exit 0 → normal permission flow (we never emit ``allow``, which would
    short-circuit other hooks).

- **Ctl mode** (argv): the `permission-change` skill calls
  ``guard.py careful|freeze|guard|deny [args]`` or ``guard.py status`` to mutate the state file
  deterministically (no LLM guesswork about JSON shape) — one skill forwarding `$ARGUMENTS`
  straight through, instead of four near-identical skills each hardcoding their own mode. The
  state file is the one the hook reads: the `SessionStart` hook
  `hooks/guard-state/pc_kanban_guard_env_hook.py` exports ``CLAUDE_PROJECT_DIR`` to `Bash`, and ctl
  mode takes the root from it (or the file from ``PC_GUARD_STATE``). With neither set it fails
  and writes nothing — it never infers a root from the shell's cwd, and has no option to name one.

State is **per session**: ``careful``, ``freeze``, ``deny`` and the `/add-dir` directories belong
to the ``session_id`` that set them and never reach another session of the same project. A
sub-agent's tool calls carry its parent's ``session_id`` (observed: a parent's `deny` blocked a
sub-agent's `Write`), so a sub-agent shares its parent's state. It lives at
``$CLAUDE_PROJECT_DIR/.claude/.guard-state.json`` (override: ``PC_GUARD_STATE``), one entry per
session, described in `hooks/guard-state/pc_kanban_guard_state.py`. The `SessionEnd` hook deletes
the session's entry and a write prunes entries untouched for seven days. Within a session it
persists across turns until explicitly lifted; every deny/ask reason says how to lift it. A state
file that is absent, unreadable or in an older format reads as "nothing added, nothing frozen".
Ctl mode reads the session from ``CLAUDE_CODE_SESSION_ID`` and fails when it is not set.

Python 3.11+, stdlib only. A hook failure must never block a tool call → unexpected errors exit 0.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_PLUGIN_ROOT / "hooks" / "guard-state"))
import pc_kanban_guard_state as gs  # noqa: E402  — le fichier d'état, partagé avec les hooks

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
PROJECT_ENV = "CLAUDE_PROJECT_DIR"

FREEZE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

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
        re.compile(r"--no-verify|--no-gpg-sign|-c\s+commit\.gpgsign=false", re.I),
    ),
]

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

CLAUDE_DIR_HAND_EDITED = {
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/settings.pinky.json",
}

CLAUDE_DIR_HAND_EDITED_PREFIXES = {".claude/memory"}


def project_root(event: dict | None = None) -> Path:
    """Resolve the hook's project root: $CLAUDE_PROJECT_DIR > event cwd > current dir.

    Ctl mode never guesses (see `ctl`). The last fallback only holds when the process cwd truly
    IS the project root; it is the last resort, so it prints what it guessed.
    """
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        return Path(proj)
    if event and event.get("cwd"):
        return Path(event["cwd"])
    cwd = Path.cwd()
    print(
        f"guard.py: no $CLAUDE_PROJECT_DIR or event cwd — guessing project root from cwd: {cwd}",
        file=sys.stderr,
    )
    return cwd


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
        return None
    if rel.as_posix() in CLAUDE_DIR_HAND_EDITED:
        return None
    if any(rel.is_relative_to(Path(p)) for p in CLAUDE_DIR_HAND_EDITED_PREFIXES):
        return None
    for prefix, message in GENERATED_TREES:
        prefix_path = Path(prefix)
        if rel == prefix_path or rel.is_relative_to(prefix_path):
            return message
    return None


def additional_dirs(state: dict, root: Path) -> list[Path]:
    """Directories this session may write to beyond the project root.

    Two sources, merged: the directories a `/add-dir` typed in this session added (`added_dirs`
    of the session's state, filed there by the `DirectoryAdded` hook), and
    `permissions.additionalDirectories` in every settings file the harness reads (project,
    project-local, user-global). A `--add-dir` passed at startup is in neither.
    """
    dirs: list[Path] = []
    for d in state.get("added_dirs") or []:
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


def outside_scope_violation(state: dict, target: str, root: Path) -> str | None:
    """Return guidance when `target` sits outside the project root AND every known additional
    directory, else None. ALWAYS ON, no opt-out — like the generated-tree guard above.

    Read-visibility is not write authorization: Claude Code natively loads ancestor `CLAUDE.md`
    files as context (walking up from the project root), and that read access says nothing about
    whether a write to that same ancestor file was ever asked for.
    """
    tgt = Path(target)
    tgt = (tgt if tgt.is_absolute() else root / tgt).resolve()
    allowed = [root.resolve(), *(d.resolve() for d in additional_dirs(state, root))]
    if any(tgt == a or tgt.is_relative_to(a) for a in allowed):
        return None
    return (
        f"`{target}` resolves outside the project root (`{root}`) and outside every directory "
        "this guard reads: a `/add-dir` typed in this session, and "
        "`permissions.additionalDirectories` in `.claude/settings.json`, "
        "`.claude/settings.local.json` and `~/.claude/settings.json`. A `--add-dir` passed at "
        "startup is not seen, so the directory may be added and still unknown here. A file "
        "Claude Code loads as ancestor context (e.g. a CLAUDE.md one level up) is read-visible by "
        "design — that is not write authorization. Confirm if this write is intended; otherwise "
        "cancel, or add the directory with `/add-dir` or `permissions.additionalDirectories`."
    )


_STOP_HANDOFF = Path(__file__).resolve().parents[3] / "hooks" / "stop_handoff.py"


def board_dirs(event: dict, root: Path) -> list[Path]:
    """Card + agent-log directories of every kanban board mounted on this machine — writable
    from any session root.

    Resolved through `stop_handoff.py`'s own CLI (`--list-kanbans` + `--list-agent-log-dirs`),
    the one place board resolution lives. A failed lookup contributes nothing: the board's files
    then fall through to the outside-scope ``ask``.
    """
    cwd = event.get("cwd") or str(root)
    dirs: list[Path] = []
    for mode in ("--list-kanbans", "--list-agent-log-dirs"):
        try:
            out = subprocess.run(
                [sys.executable, str(_STOP_HANDOFF), mode],
                capture_output=True,
                text=True,
                timeout=3,
                cwd=cwd,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0:
            continue
        for line in out.stdout.splitlines():
            line = line.strip()
            if line:
                dirs.append(Path(line).resolve())
    return dirs


def is_session_scratchpad(target: str) -> bool:
    """True when `target` sits inside a Claude Code session scratchpad directory.

    The harness announces this path per session (the "Scratchpad directory" line in the
    Environment banner) but does not pass it to hooks directly — no env var, no PreToolUse event
    field — so it is recognized by its own stable shape instead:
    ``<tmpdir>/claude-<uid>/<sanitized-cwd>/<session-id>/scratchpad/...``. The harness's own banner
    already tells the agent this directory "can generally be used without permission prompts";
    without this carve-out, the outside-scope guard treats it like any other out-of-repo path and
    asks anyway — a real problem for a `context: fork` skill (no channel back to answer an ask).
    """
    tgt = Path(target)
    tgt = tgt if tgt.is_absolute() else Path.cwd() / tgt
    try:
        tgt = tgt.resolve()
    except OSError:
        return False
    # Ne pas ancrer sur `tempfile.gettempdir()` : sur macOS il suit `$TMPDIR`, dont la racine
    # diffère de celle où le harness écrit le scratchpad. On reconnaît la forme du chemin à la
    # place, indépendamment de sa racine.
    parts = tgt.parts
    for i, part in enumerate(parts):
        if re.fullmatch(r"claude-\d+", part) and "scratchpad" in parts[i + 1 :]:
            return True
    return False


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
    """Core hook decision: ask on careful, deny on freeze escape, else None (let flow proceed).

    The state read is the entry of the event's `session_id` — nothing set by another session.
    """
    state = gs.load_session(gs.state_path(root), event.get("session_id"))
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
            message = generated_tree_violation(target, root)
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

            memo: list[list[Path]] = []

            def board() -> list[Path]:
                if not memo:
                    memo.append(board_dirs(event, root))
                return memo[0]

            def on_board() -> bool:
                tgt = Path(target)
                tgt = (tgt if tgt.is_absolute() else root / tgt).resolve()
                return any(tgt == d or tgt.is_relative_to(d) for d in board())

            scratchpad = is_session_scratchpad(target)

            message = outside_scope_violation(state, target, root)
            if message:
                if on_board() or scratchpad:
                    return None
                return _decision("ask", message)

            if state.get("freeze"):
                scope = frozen_violation(target, state["freeze"], root)
                if scope and not on_board() and not scratchpad:
                    return _decision(
                        "deny",
                        f"freeze: edits are locked to `{scope}`; `{target}` is outside it. "
                        f"Lift the lock with `/permission-change freeze off`.",
                    )
    return None


def run_hook() -> int:
    """Read the PreToolUse event from stdin, emit a decision when one applies, never raise."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0
    try:
        sid = event.get("session_id")
        if not isinstance(sid, str) or not sid:
            print(
                "guard.py: PreToolUse event without a usable session_id — careful, freeze, "
                "deny and added directories are not applied to this call.",
                file=sys.stderr,
            )
        decision = decide(event, project_root(event))
    except Exception:
        return 0
    if decision is not None:
        print(json.dumps(decision))
    return 0


def _onoff(arg: str | None, default: bool = True) -> bool:
    """Parse an on/off argument; absent → `default`."""
    if arg is None:
        return default
    return arg.strip().lower() not in {"off", "false", "0", "no"}


def format_status(state: dict, session_id: str) -> str:
    """One-line human summary of the session's guards, added directories and short session id."""
    careful = "on" if state.get("careful") else "off"
    freeze = state.get("freeze") or []
    freeze_str = ", ".join(freeze) if freeze else "off"
    deny = state.get("deny") or []
    deny_str = ", ".join(deny) if deny else "off"
    added = len(state.get("added_dirs") or [])
    return (
        f"careful: {careful} · freeze: {freeze_str} · deny: {deny_str}"
        f" · added dirs: {added} · session: {session_id[:8]}"
    )


USAGE = (
    "usage: guard.py careful [on|off]\n"
    "       guard.py freeze <dir>... | off\n"
    "       guard.py guard <dir>... | on | off\n"
    "       guard.py deny <dir>... | off\n"
    "       guard.py status"
)


def ctl(argv: list[str]) -> int:
    """Mutate the state file from a skill invocation; print the resulting status.

    A single `permission-change` skill forwards `$ARGUMENTS` straight through — one positional
    mode word (`careful`/`freeze`/`guard`/`deny`/`status`) picks the branch below, instead of four
    near-duplicate skills each hardcoding their own mode. The change is computed on the entry
    read under the state lock, so a concurrent writer of the same session (a `/add-dir` filed by
    the `DirectoryAdded` hook, another `freeze`) is never overwritten with a stale value.

    The state file is `$PC_GUARD_STATE`, else `$CLAUDE_PROJECT_DIR/.claude/.guard-state.json`;
    with neither set the root is unknown and nothing is written.
    """
    mode = argv[0]
    rest = argv[1:]
    if mode not in {"status", "careful", "freeze", "deny", "guard"}:
        print(f"unknown mode: {mode}\n{USAGE}", file=sys.stderr)
        return 2
    if mode in {"freeze", "deny"} and not rest:
        print(f"usage: guard.py {mode} <dir>... | off", file=sys.stderr)
        return 2
    session_id = os.environ.get(SESSION_ENV)
    if not session_id:
        print(
            f"guard.py: {SESSION_ENV} is not set — the guard state is per session and this "
            "command cannot tell which session to read or change. Run it from the session's "
            "own shell.",
            file=sys.stderr,
        )
        return 1
    project_dir = os.environ.get(PROJECT_ENV)
    override = os.environ.get(gs.STATE_ENV)
    if override:
        path = Path(override)
    elif project_dir:
        path = gs.state_path(Path(project_dir))
    else:
        print(
            f"guard.py: the project root is unknown for this session — {PROJECT_ENV} is not "
            "set, so the guard would not read the state written from here and nothing was "
            "written. It is exported at session start and `/reload-plugins` does not replay "
            "that: if the plugin's hooks are not loaded yet run `/reload-plugins`, then "
            "restart the session (`/clear`).",
            file=sys.stderr,
        )
        return 1

    if mode == "status":
        print(format_status(gs.load_session(path, session_id), session_id))
        return 0

    def change(entry: dict) -> None:
        if mode == "careful":
            entry["careful"] = _onoff(rest[0] if rest else None)
        elif mode in {"freeze", "deny"}:
            off = rest[0].lower() in {"off", "none", "clear"}
            entry[mode] = [] if off else list(rest)
        elif mode == "guard":
            if not _onoff(rest[0] if rest else None):
                entry["careful"], entry["freeze"] = False, []
            else:
                entry["careful"] = True
                dirs = [a for a in rest if a.lower() not in {"on", "true", "1", "yes"}]
                if dirs:
                    entry["freeze"] = dirs
        else:
            raise AssertionError(mode)

    saved = gs.update_session(path, session_id, change)
    print(format_status(saved, session_id))
    return 0


def main(argv: list[str]) -> int:
    """Dispatch: argv present → ctl mode; otherwise → hook mode (stdin).

    Ctl mode has no option to name the root: it is the session's `$CLAUDE_PROJECT_DIR`, the one
    the hook reads. A `--root` option, anywhere in argv, is refused — never read as a directory
    to freeze.
    """
    if not argv:
        return run_hook()
    if any(a == "--root" or a.startswith("--root=") for a in argv):
        print(
            "guard.py: unknown option --root — the project root is the session's "
            f"{PROJECT_ENV}, it cannot be overridden.\n{USAGE}",
            file=sys.stderr,
        )
        return 2
    return ctl(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
