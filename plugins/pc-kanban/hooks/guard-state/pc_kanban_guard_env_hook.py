#!/usr/bin/env python3
"""SessionStart hook — export the session's project root to `Bash` as `CLAUDE_PROJECT_DIR`.

The PreToolUse guard resolves the project root from `$CLAUDE_PROJECT_DIR` and reads
`<root>/.claude/.guard-state.json`. The harness hands that variable to hooks but not to the
`Bash` tool, so `guard.py` called from `Bash` has no root of its own and would write the state
somewhere the guard never reads. `SessionStart` is given `CLAUDE_ENV_FILE`: a shell file the
harness sources before each `Bash` command. This hook appends one `export` line to it, with the
root `guard.project_root` resolves, so the CLI and the guard read the same state file.

The file is opened for append — lines other hooks wrote stay. Every `SessionStart` (startup,
resume, clear, compact) appends one line; the last one wins. Without `CLAUDE_PROJECT_DIR` or an
event `cwd` there is no root to export — a cwd guess is not written into the `Bash` environment
for the whole session — so nothing is exported.

Nothing is printed on stdout; a failure prints one stderr line. A missing or unwritable
`CLAUDE_ENV_FILE`, or a failure while resolving the root or writing, does nothing and exits 0.
The import of `guard` is at module level and sits outside that guarantee.

Python 3.11+, stdlib only.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PLUGIN / "skills" / "permission-change" / "scripts"))
import guard  # noqa: E402  — seulement pour `project_root`, l'unique endroit qui résout la racine

ENV_FILE = "CLAUDE_ENV_FILE"
EXPORTED = "CLAUDE_PROJECT_DIR"


def build_export_line(root: Path) -> str:
    """The shell line that exports `root` as the project directory.

    Args:
        root: The session's project root.

    Returns:
        One `export` line, shell-quoted, newline-terminated.
    """
    return f"export {EXPORTED}={shlex.quote(str(root))}\n"


def main() -> int:
    """Append the export line to `CLAUDE_ENV_FILE`; always exit 0, print nothing.

    Returns:
        0, whatever happens.
    """
    env_file = os.environ.get(ENV_FILE)
    if not env_file:
        return 0
    try:
        try:
            event = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            event = {}
        if not (os.environ.get(EXPORTED) or event.get("cwd")):
            print(
                f"guard env hook: neither {EXPORTED} nor the event cwd is given, "
                "nothing exported",
                file=sys.stderr,
            )
            return 0
        line = build_export_line(guard.project_root(event))
        with open(env_file, "ab+") as fh:
            # fichier non vide : finit-il par un saut de ligne ?
            if fh.seek(0, os.SEEK_END):
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    fh.write(b"\n")
            fh.write(line.encode("utf-8"))
    except Exception as exc:  # a hook must never break a session on its own bug
        print(f"guard env hook: skipped ({exc!r})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
