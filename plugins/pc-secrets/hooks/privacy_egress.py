#!/usr/bin/env python3
"""PreToolUse hook — block sensitive-data egress to the cloud model.

Defines "done": on a ``Read`` — or a read-like ``Bash`` command (cat/head/tail/…, a channel a
permissive baseline often pre-allows prompt-free) — whose target file CONTENT carries
secret-shaped data, return ``permissionDecision: "deny"`` — that is the boundary where the content
has not yet entered the context (hence not yet been sent to the cloud model). Other tools and
clean files yield no decision (the flow proceeds). The hook never raises: a guard must not block a
tool call on its own bug. Honest scope: it stops the ACCIDENT, not determined extraction —
grep/awk/interpreters are open channels by design; the real barriers stay a repo's own pre-commit
gates and its `permissions.deny` baseline.

Detection reuses `secret_patterns.py` — the one "secret shapes" module, shared with whatever else
needs the same detector (minimal-code: no second pattern list). Lines opting out with
``# pragma: allowlist secret`` (or ``gitleaks:allow``) are honoured exactly like the pre-commit
secret hooks, so a deliberately-allowlisted fixture is not blocked.

Path-level protection (``.env``, ``~/.ssh``, ``~/.aws``) belongs to a repo's own
``permissions.deny`` baseline; this hook is the *content* level — real creds inside an otherwise
allowed file.

The agent's own scope is never scanned, whatever its content: a target under the resolved config
dir (``CLAUDE_CONFIG_DIR``, else ``~/.claude/``) or carrying a ``.claude`` directory component —
memory notes included — is skipped before any content check, on both channels (``Read``, a
read-like ``Bash`` command). Blocking the agent on its own journal and settings is all friction,
no protection, and the repeated false positives end up getting the guard disabled.

The same script also answers the band mod (``hooks/secrets-hold``): ``--verdict`` prints the guard's
judgement of a ``Read`` event as JSON (which lines, which choices make sense) and ``--pragma``
writes the allowlist marker on the lines it is given. The guard stays the one source of the
verdict; the mod only presents it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from secret_patterns import secret_kinds  # noqa: E402

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

MAX_SCAN_BYTES = (
    256 * 1024
)  # scan the head of large files; never load a multi-GB dump into the hook
_ALLOWLIST_MARKERS = ("allowlist secret", "gitleaks:allow")
MAX_REPORTED_LINES = (
    10  # bound the line list in the deny reason (hook output stays bounded)
)

PRAGMA = "# pragma: allowlist secret"
# Formats où un `#` en fin de ligne est un commentaire sans effet sur la valeur : le pragma n'est proposé que là
# (ni `.ini` ni `.properties`, où il entrerait dans la valeur ; ni un langage où `#` est une erreur de syntaxe).
_HASH_COMMENT_SUFFIXES = {
    "",
    ".py",
    ".sh",
    ".bash",
    ".zsh",
    ".yaml",
    ".yml",
    ".toml",
    ".conf",
    ".txt",
    ".md",
    ".env",
    ".rb",
    ".pl",
    ".r",
}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
POOL_PROBE_TIMEOUT = (
    1.0  # secondes ; le mod n'attend pas plus pour proposer « delegate offline »
)

_READ_CMDS = {"cat", "head", "tail", "less", "more", "strings"}
MAX_BASH_TARGETS = 8  # bound the per-call scan cost


def _decision(kind: str, reason: str) -> dict:
    """Wrap a PreToolUse permission decision in the harness-expected envelope."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": kind,
            "permissionDecisionReason": reason,
        }
    }


def is_git_tracked(path: Path) -> bool:
    """True if `path` is tracked by git. Tracking alone is NOT vetting — see `is_vetted`."""
    try:
        r = subprocess.run(
            [
                "git",
                "-C",
                str(path.parent),
                "ls-files",
                "--error-unmatch",
                "--",
                path.name,
            ],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def is_vetted(path: Path) -> bool:
    """True when `path` is git-tracked AND identical to HEAD — content that actually cleared a
    pre-commit secret gate. Re-scanning it would be redundant. A staged or modified tracked file
    has passed NO gate (pre-commit runs at commit time), so it is scanned exactly like an
    untracked one. Outside a git repo (or on git failure) → not vetted → scanned."""
    if not is_git_tracked(path):
        return False
    try:
        r = subprocess.run(
            ["git", "-C", str(path.parent), "status", "--porcelain", "--", path.name],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and not r.stdout.strip()


def secret_lines(path: Path) -> list[tuple[int, str]]:
    """(1-based line number, reason) for each (head) line carrying secret-shaped data, skipping
    allowlisted lines. Empty = clean."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:MAX_SCAN_BYTES]
    except (OSError, ValueError):
        return []  # unreadable / binary → not our call; let the Read itself deal with it
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if any(marker in line for marker in _ALLOWLIST_MARKERS):
            continue
        kinds = secret_kinds(line)
        if kinds:
            hits.append((lineno, " + ".join(kinds)))
    return hits


def bash_read_targets(command: str) -> list[Path]:
    """Existing files a simple read-like Bash segment (cat/head/tail/less/more/strings) would dump
    into the context. Conservative token scan, NOT a shell parser."""
    targets: list[Path] = []
    for segment in re.split(r"[|;&]+", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:  # unbalanced quotes etc. — best effort
            tokens = segment.split()
        if not tokens or Path(tokens[0]).name not in _READ_CMDS:
            continue
        for tok in tokens[1:]:
            if tok.startswith("-"):
                continue
            p = Path(tok).expanduser()
            if p.is_file():
                targets.append(p)
    return targets[:MAX_BASH_TARGETS]


def _is_excluded(path: Path) -> bool:
    """True for the agent's own scope, never scanned: a path with a ``.claude`` directory
    component, or under the resolved config dir (``CLAUDE_CONFIG_DIR``, else ``~/.claude/``) —
    which mechanically covers the default ``projects/<project>/memory`` silo. Lexical only (no
    filesystem call), so it stays the cheapest check in ``decide``.

    Args:
        path: The candidate target, as it reached the hook.

    Returns:
        True when the guard must skip this target on every channel.
    """
    expanded = path.expanduser()
    if ".claude" in expanded.parts:
        return True
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()
    return expanded.is_relative_to(config_dir)


def flagged(event: dict) -> tuple[Path, list[tuple[int, str]]] | None:
    """The first target of a Read — or read-like Bash — event whose content carries secret-shaped data.

    Same boundary for both channels: the content has not yet entered the context.

    Args:
        event: The PreToolUse event.

    Returns:
        The path with its flagged ``(line number, reason)`` pairs, or None when nothing is flagged.
    """
    tool = event.get("tool_name")
    tool_input = event.get("tool_input") or {}
    if tool == "Read":
        file_path = tool_input.get("file_path")
        targets = [Path(file_path)] if file_path else []
    elif tool == "Bash":
        targets = bash_read_targets(tool_input.get("command", ""))
    else:
        return None
    for path in targets:
        if _is_excluded(path):
            continue  # the agent's own notes/config — never scanned
        if is_vetted(path):
            continue  # cleared the commit gate — never re-block the repo's own files
        hits = secret_lines(path)
        if hits:
            return path, hits
    return None


def decide(event: dict) -> dict | None:
    """Deny a Read — or a read-like Bash command — whose target file contains sensitive data.

    Args:
        event: The PreToolUse event.

    Returns:
        The deny envelope, or None when the flow may proceed.
    """
    found = flagged(event)
    if found is None:
        return None
    path, hits = found
    shown = "; ".join(f"line {n} ({why})" for n, why in hits[:MAX_REPORTED_LINES])
    if len(hits) > MAX_REPORTED_LINES:
        shown += f"; … (+{len(hits) - MAX_REPORTED_LINES} more)"
    return _decision(
        "deny",
        f"Privacy guard: `{path}` contains secret-shaped data — {shown} — reading it "
        f"would send it to the cloud model. Run the task locally instead, or mark a false "
        f"positive with `# pragma: allowlist secret` on the offending line.",
    )


def pool_reachable() -> bool:
    """True when the local model server behind ``offline`` (pi's ``mlx`` provider) answers.

    Only a loopback server counts: ``offline`` promises the content stays on the machine.

    Returns:
        False on a missing config or provider, a non-local host or no answer — never raises.
    """
    try:
        config = Path.home() / ".pi" / "agent" / "models.json"
        base = json.loads(config.read_text(encoding="utf-8"))["providers"]["mlx"][
            "baseUrl"
        ]
        parsed = urlparse(base)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.hostname not in _LOCAL_HOSTS
        ):
            return False  # pas de `file:`, pas de serveur distant tiré d'une config
        with urllib.request.urlopen(
            f"{base.rstrip('/')}/models", timeout=POOL_PROBE_TIMEOUT
        ):  # nosec B310 — schéma vérifié ci-dessus
            return True
    except urllib.error.HTTPError:
        return True  # il répond, même par un refus
    except Exception:
        return False


def file_digest(path: Path) -> str:
    """SHA-256 of the file's bytes, binding a verdict to the content that was judged.

    Args:
        path: The file to fingerprint.

    Returns:
        The hex digest, empty when the file cannot be read.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _pragma_ok(path: Path) -> bool:
    """True when a trailing ``#`` keeps this file valid: a known format, and not a symlink."""
    return path.suffix.lower() in _HASH_COMMENT_SUFFIXES and not path.is_symlink()


def verdict(event: dict) -> dict:
    """The guard's judgement of a ``Read`` event, shaped for the band that offers the choices.

    Args:
        event: The PreToolUse event.

    Returns:
        A dict with ``flagged`` False alone, or, when flagged, the path, its lines, the digest of
        the judged content and which choices make sense (``pragma``: a ``#`` comment keeps the file
        valid; ``offline``: the local pool answers).
    """
    if event.get("tool_name") != "Read":
        return {"flagged": False}
    found = flagged(event)
    if found is None:
        return {"flagged": False}
    path, hits = found
    return {
        "flagged": True,
        "path": str(path),
        "lines": [n for n, _ in hits],
        "digest": file_digest(path),
        "pragma": _pragma_ok(path),
        "offline": pool_reachable(),
    }


def add_pragma(path: Path, lines: list[int], digest: str) -> int:
    """Write the allowlist marker at the end of the given 1-based lines, the rest left byte for byte.

    Args:
        path: The file to mark.
        lines: 1-based line numbers, as listed by ``verdict``.
        digest: The digest ``verdict`` gave for the judged content.

    Returns:
        How many lines were marked; 0 for a file a ``#`` would break or a symlink, an unreadable
        file, a file changed since the verdict, or lines already allowlisted, out of range,
        continued with a backslash, or no longer carrying a secret.
    """
    if not _pragma_ok(path) or not digest or file_digest(path) != digest:
        return 0
    try:
        parts = path.read_bytes().decode("utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return 0
    marked = 0
    for n in lines:
        if not 1 <= n <= len(parts):
            continue
        line = parts[n - 1]
        body = line.splitlines()[0]
        if (
            any(m in line for m in _ALLOWLIST_MARKERS)
            or not secret_kinds(line)
            or body.endswith("\\")
        ):
            continue
        parts[n - 1] = f"{body}  {PRAGMA}{line[len(body) :]}"
        marked += 1
    if marked:
        path.write_bytes("".join(parts).encode("utf-8"))
    return marked


def run_hook() -> int:
    """Read the PreToolUse event from stdin, emit a decision when one applies, never raise."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0
    try:
        decision = decide(event)
    except Exception:  # a guard must never block a tool call on its own bug
        return 0
    if decision is not None:
        print(json.dumps(decision))
    return 0


def main(argv: list[str]) -> int:
    """Run the hook, or one of the mod's helper modes (``--verdict``, ``--pragma <path> <digest> <line>...``).

    Args:
        argv: The arguments after the script name.

    Returns:
        The process exit code.
    """
    if argv[:1] == ["--verdict"]:
        try:
            event = json.loads(sys.stdin.read() or "{}")
            print(json.dumps(verdict(event)))
        except Exception:  # the mod treats no answer as « no band »
            return 1
        return 0
    if (
        argv[:1] == ["--pragma"]
        and len(argv) >= 4
        and all(a.isascii() and a.isdecimal() for a in argv[3:])
    ):
        print(add_pragma(Path(argv[1]), [int(a) for a in argv[3:]], argv[2]))
        return 0
    return run_hook()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
