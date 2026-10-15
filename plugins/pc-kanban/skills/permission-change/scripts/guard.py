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
    first, so a target further out gets its ``ask``, never this ``deny``. A `Bash`
    command that both looks like a write (`>`, `tee`, `sed -i`, `mv`, `cp`, `rm`, `git commit`…)
    and names an absolute/`~` path, a `cd` target or a `git -C` dir outside the frozen directories
    returns ``ask`` (a speed bump, never a deny). Heredoc bodies and quoted strings that are text
    are dropped before the search, a candidate that does not start with a real directory is not a
    path, `N>&M` / `>/dev/null` are not writes; code handed to a shell, `ssh`, `-c`, `eval` or a
    runner stays analysed. HONEST LIMIT: a string heuristic — a computed path or a write verb
    outside the list passes. The rules, what stays code, the accepted over-asks and the full
    limit are in `skills/permission-change/SKILL.md` (the authority). System paths (`/usr`,
    `/tmp`, `/dev`…), boards and the session scratchpad never trigger it.
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


_BASH_WRITE_MARKER = re.compile(
    r">|\btee\b|\bsed\s+(?:-\w+\s+)*-\w*i|"
    r"\b(?:mv|cp|rm|touch|mkdir|ln|install|rsync|truncate|unlink)\b|"
    r"\bos\.(?:remove|rename|replace|rmdir|makedirs)\b|\bshutil\.(?:copy\w*|move|rmtree)\b|"
    r"\bgit\b[^\n;|&]*?\b(?:commit|checkout|switch|restore|apply|add|reset|merge|rebase|push)\b"
)
# A quoted string that opens with a write command (`rm …`, `sudo git commit …`) is code handed to a
# wrapper (`watch`, `parallel`, `<<<`), not text. Anchored: one match per string.
_BASH_WRITE_COMMAND = re.compile(
    r"\s*(?:\b(?:sudo|env|nohup|time)\b\s+)*(?:" + _BASH_WRITE_MARKER.pattern + ")"
)
_BASH_PATH = re.compile(r"(?<![\w:./-])(~?/[^\s'\";|&<>()`$]*)")
_BASH_DIR_ARG = re.compile(r"(?:\bcd|\bgit\s+-C|--git-dir=?)\s*([^\s'\";|&<>()`$]+)")
_BASH_SYSTEM_PREFIXES = (
    "/usr", "/bin", "/sbin", "/opt", "/dev", "/proc", "/tmp",
    "/private/tmp",
)  # fmt: skip
_BASH_HEREDOC = re.compile(
    r"(?<!<)<<(?!<)(?P<dash>-?)[ \t]*\\?(?P<q>['\"]?)(?P<delim>[A-Za-z_]\w*)(?P=q)"
)
# A word that makes the heredoc on the same line a program: a shell, `ssh`, `eval`, `source`, `at`,
# `su`, or an input descriptor read as a program.
_BASH_INTERPRETER = re.compile(
    r"\b(?:sh|bash|zsh|ksh|dash|ash|csh|tcsh|fish|ssh|eval|source|at|su)\b"
    r"|/dev/stdin\b|/dev/fd/\d+"
)
_BASH_ARITHMETIC = re.compile(r"\$?\(\((?:[^()]|\([^()]*\))*\)\)")
_BASH_QUOTE = re.compile(r"""'[^']*'|"[^"\\]*(?:\\.[^"\\]*)*\"""")
_BASH_QUOTE_OR_ESCAPE = re.compile(r"""['"\\]""")
# A word that makes the quoted strings after it code that runs: `-c` (also `-lc`…), `eval`, a shell
# word,
# `ssh`, a wrapper that runs its argument (`watch`, `parallel`), a here-string (`<<<`).
_BASH_CODE_OPENER = re.compile(
    r"(?<![\w-])-[A-Za-z]*c\b|\b(?:eval|ssh|sh|bash|zsh|ksh|dash|watch|parallel)\b|<<<"
)
# A pipe into a shell, and a command separator that is not a `N>&M` / `&>` redirection.
_BASH_PIPE_TO_SHELL = re.compile(
    r"\|\s*(?:(?:sudo|env|time|nohup)\s+)*(?:\S*/)?\b(?:sh|bash|zsh|ksh|dash)\b"
)
_BASH_SEPARATOR = re.compile(r";|\n|(?<![>&])&(?!>)")
_BASH_FD_DUP = re.compile(r">&\d*-?|&>>?")
_BASH_NEUTRAL_REDIRECT = re.compile(
    r"\d*>&\d+|&>\s*/dev/null(?![\w./-])|\d*>>?\s*/dev/null(?![\w./-])"
)


def _continues(line: str) -> bool:
    """Return whether `line` does not end its command.

    It does not when it ends with a backslash continuation (an odd number of trailing
    backslashes), a pipe, `&&`, `|&` or a redirection operator.

    Args:
        line: One physical line of the command.

    Returns:
        Whether the next physical line belongs to the same logical line.
    """
    if (len(line) - len(line.rstrip("\\"))) % 2:
        return True
    return line.rstrip().endswith(("|", "&&", "|&", ">", "<"))


def _drop_heredoc_bodies(command: str) -> str | None:
    """Return `command` without its heredoc bodies and terminator lines, else None.

    The opening line stays: it carries the redirect target (`cat <<'EOF' > /abs/z`). A body is
    kept, not dropped, when the command that reads it may be a shell: a word `sh`/`bash`/`ssh`/
    `eval`… stands anywhere on the opening line, before or after the `<<` (`bash <<EOF`,
    `cat <<EOF | sh`), or the opening line sits inside a `$(…)` or backtick substitution that has
    such a word (`bash -c "$(` newline `cat <<EOF`). The opening line is the logical line:
    physical lines joined by a continuation (`_continues`) count as one, and the body starts
    after the last of them. A terminator matches a whole line (`<<-` also strips leading tabs).
    A delimiter is an identifier and arithmetic expansions are skipped, so `$((x << 4))` opens no
    heredoc. None means a heredoc has no terminator.

    Args:
        command: The Bash command line(s).

    Returns:
        The command minus the heredoc bodies, or None when a heredoc is never terminated.
    """
    lines = command.split("\n")
    kept: list[str] = []
    pending: list[tuple[str, bool, bool]] = []
    depth = 0
    ticks_open = False
    sub_has_shell = False
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if pending:
            delim, strip_tabs, is_code = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == delim:
                pending.pop(0)
            elif is_code:
                kept.append(line)
            continue
        logical = [line]
        while i < len(lines) and _continues(line):
            line = lines[i]
            logical.append(line)
            i += 1
        kept.extend(logical)
        opening = _BASH_ARITHMETIC.sub(" ", "\n".join(logical))
        has_shell = _BASH_INTERPRETER.search(opening) is not None
        was_in_substitution = depth > 0 or ticks_open
        depth = max(0, depth + opening.count("(") - opening.count(")"))
        ticks_open = ticks_open != bool(opening.count("`") % 2)
        in_substitution = depth > 0 or ticks_open
        if in_substitution:
            sub_has_shell = (sub_has_shell and was_in_substitution) or has_shell
        else:
            sub_has_shell = False
        is_code = has_shell or (in_substitution and sub_has_shell)
        pending = [
            (m["delim"], bool(m["dash"]), is_code)
            for m in _BASH_HEREDOC.finditer(opening)
        ]
    return None if pending else "\n".join(kept)


def _code_scope(segment: str, scope: bool) -> bool:
    """Return whether the quoted strings after `segment` are code that runs.

    `segment` is the unquoted text between two quoted strings. A command separator (`;`, `|`, `&`,
    newline, not a `N>&M` redirection) after the last code opener (`-c`, `eval`, `ssh`) ends the
    scope, an opener after the last separator opens it, and a segment with neither leaves `scope`
    as it was. Options between the opener and the string do not matter.

    Args:
        segment: Unquoted command text.
        scope: The state before this segment.

    Returns:
        The state after this segment.
    """
    segment = _BASH_FD_DUP.sub(" ", segment)
    last_open = max(
        (m.start() for m in _BASH_CODE_OPENER.finditer(segment)), default=-1
    )
    last_sep = max(segment.rfind(c) for c in ";|&\n")
    if last_open == last_sep:
        return scope
    return last_open > last_sep


def _feeds_shell(segment: str, ahead: bool) -> bool:
    """Return whether a pipe into a shell comes before the next separator, from `segment` on.

    `segment` is unquoted text, `ahead` the answer for the text that follows it. A `|` does not
    end the search (`echo 'cmd' | sh`), a `;`, `&` or newline does.

    Args:
        segment: Unquoted command text.
        ahead: The answer for the text after `segment`.

    Returns:
        True when a pipe into a shell is reached before a separator.
    """
    pipe = _BASH_PIPE_TO_SHELL.search(segment)
    sep = _BASH_SEPARATOR.search(segment)
    if pipe is not None and (sep is None or pipe.start() < sep.start()):
        return True
    return False if sep is not None else ahead


def _drop_quoted_text(command: str) -> str | None:
    """Return `command` with every quoted string that is not a path replaced by `""`, else None.

    A quoted string is kept when it starts with `/` or `~`, or, double-quoted, with `$` or when it
    holds a `$(` or a backtick (a substitution runs), or when it is code that runs: it comes after
    `-c` (also `-lc`…), `eval`, a word `sh`/`bash`/`ssh`…, `watch`, `parallel` or `<<<` in the same
    command,
    whatever options and strings sit in between (see `_code_scope`), or it is piped into a
    shell later in the same command (`echo '…' | sh`, see `_feeds_shell`), or its first word is a
    write command (`rm`, `mv`, `git commit`…, optionally after `sudo`/`env`/`nohup`/`time`). A
    backslash outside quotes escapes the next character. Two linear passes; None means a quote
    is never closed.

    Args:
        command: The Bash command, heredoc bodies already removed.

    Returns:
        The command with non-path quoted text blanked, or None when a quote is unclosed.
    """
    out: list[str] = []
    slots: list[tuple[int, str, bool]] = []
    segments: list[str] = []
    current: list[str] = []
    i = 0
    code_scope = False
    while True:
        m = _BASH_QUOTE_OR_ESCAPE.search(command, i)
        if m is None:
            out.append(command[i:])
            current.append(command[i:])
            segments.append(" ".join(current))
            break
        piece = command[i : m.start()]
        out.append(piece)
        current.append(piece)
        code_scope = _code_scope(piece, code_scope)
        if m.group() == "\\":
            out.append(command[m.start() : m.start() + 2])
            i = m.start() + 2
            continue
        q = _BASH_QUOTE.match(command, m.start())
        if q is None:
            return None
        text = q.group()
        body = text[1:-1]
        starts = ("/", "~", "$") if text[0] == '"' else ("/", "~")
        runs = text[0] == '"' and ("$(" in body or "`" in body)
        code = code_scope or _BASH_WRITE_COMMAND.match(text, 1) is not None
        slots.append((len(out), text, body.startswith(starts) or runs or code))
        out.append("")
        segments.append(" ".join(current))
        current = []
        i = q.end()
    ahead = False
    for k in range(len(slots), 0, -1):
        ahead = _feeds_shell(segments[k], ahead)
        index, text, kept = slots[k - 1]
        out[index] = text if kept or ahead else '""'
    return "".join(out)


def _names_real_directory(path: str) -> bool:
    """Return whether an absolute `path` starts with a directory that exists under `/`.

    Args:
        path: An absolute path, `~` already expanded.

    Returns:
        True when its first component is a directory under `/`; False for `/` alone.
    """
    first = next((part for part in path.split("/") if part), None)
    return first is not None and os.path.isdir("/" + first)


def bash_write_escapes(command: str, freeze_dirs: list[str], root: Path) -> list[str]:
    """Return the paths a write-looking Bash `command` names outside every frozen directory.

    Empty when there is no freeze, no write marker, or every named path is inside a frozen
    directory or under a system prefix. Board and scratchpad carve-outs are the caller's job.

    Text is not a path: heredoc bodies and quoted strings that are text are dropped first (code
    handed to a shell, `ssh`, `-c`, `eval` or a runner is kept, see `SKILL.md`), and `N>&M` /
    `>/dev/null` redirections are not writes. An absolute candidate whose first component is not
    an existing directory under `/` is not a path. A command whose heredoc or quote is never
    closed is analysed as written, none of these rules applied.
    """
    if not freeze_dirs:
        return []
    without_bodies = _drop_heredoc_bodies(command)
    stripped = None if without_bodies is None else _drop_quoted_text(without_bodies)
    text = command if stripped is None else _BASH_NEUTRAL_REDIRECT.sub(" ", stripped)
    if not _BASH_WRITE_MARKER.search(text):
        return []
    candidates = _BASH_PATH.findall(text) + _BASH_DIR_ARG.findall(text)
    escapes: list[str] = []
    for raw in dict.fromkeys(candidates):
        path = os.path.expanduser(raw)
        if path.startswith(_BASH_SYSTEM_PREFIXES):
            continue
        if (
            stripped is not None
            and path.startswith("/")
            and not _names_real_directory(path)
        ):
            continue
        if frozen_violation(path, freeze_dirs, root) and path not in escapes:
            escapes.append(path)
    return escapes


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

    if tool == "Bash" and state.get("freeze"):
        escapes = bash_write_escapes(
            tool_input.get("command", ""), state["freeze"], root
        )
        if escapes:
            board_paths = board_dirs(event, root)

            def exempt(raw: str) -> bool:
                tgt = Path(raw)
                tgt = (tgt if tgt.is_absolute() else root / tgt).resolve()
                return is_session_scratchpad(str(tgt)) or any(
                    tgt == d or tgt.is_relative_to(d) for d in board_paths
                )

            escapes = [e for e in escapes if not exempt(e)]
        if escapes:
            return _decision(
                "ask",
                f"freeze: this command looks like a write outside the locked scope "
                f"(`{', '.join(escapes)}`). If the card genuinely needs that repo, add its slug "
                f"to the card's `repos:` and relay with `spawn-workers` instead of writing "
                f"across (finishing-a-card § Touching a repo the card does not list).",
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
