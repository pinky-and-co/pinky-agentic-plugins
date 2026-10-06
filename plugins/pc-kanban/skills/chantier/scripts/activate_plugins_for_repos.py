#!/usr/bin/env python3
"""activate_plugins_for_repos.py — chantier's own plugin-activation call.

Card selection (chantier SKILL.md step 7, "Repo-scope") freezes write access
to the card's resolved repo paths, but doesn't necessarily change the
session's cwd — so sync_plugin_scope.py's CwdChanged hook may never fire for
repos a card is about to touch even though it's about to work there. This
script covers that gap: given the paths resolve_repos.sh already resolved,
activate every project-scoped plugin whose projectPath matches one of them,
via the same claude plugin enable -s local path plugin_scope_lib's own
enable_plugin_local() uses everywhere else.

Dedup is checked against enabledPlugins in the session's actual
settings.local.json (via plugin_scope_lib.settings_local_plugin_ids), not a
separate session-state file: that's the ground truth Claude Code itself
reads, and it's what sync_plugin_scope.py's own CwdChanged calls may have
already changed earlier in this same session — reading the real file, rather
than shadow state, keeps the two mechanisms from disagreeing.

Prints one plugin id per line for everything newly enabled this call, empty
stdout if nothing changed — that's the signal SKILL.md's Repo-scope bullet
reads to decide whether the /reload-plugins interrupt is needed. A plugin
already on (`already_enabled`) is a stderr note and skipped; a real enable
failure is a stderr error and skipped — neither aborts the rest, same
posture as sync_plugin_scope.py's own activate_for_path().

Usage: activate_plugins_for_repos.py --session-id "${CLAUDE_SESSION_ID}" <resolved-repo-path> [<path>...]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pc_kanban_plugin_scope_lib import (
    enable_plugin_local,
    load_state,
    matching_plugin_ids,
    settings_local_plugin_ids,
)


def session_open_path(session_id: str) -> Path | None:
    """The directory `claude plugin enable -s local` resolves "local" against
    — the one the session was opened in, recorded at SessionStart in the
    session's state file. None when the session is unknown: the shell's cwd
    drifts between calls, so it never stands in for it."""
    settings_local = load_state(session_id)["settings_local_path"]
    return Path(settings_local).parent.parent if settings_local else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--session-id", default="")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    if not args.paths:
        print(
            "usage: activate_plugins_for_repos.py --session-id <id> <resolved-repo-path>...",
            file=sys.stderr,
        )
        return 2

    open_path = session_open_path(args.session_id) if args.session_id else None
    if open_path is None:
        print(
            "activate_plugins_for_repos: no --session-id, or no state recorded for"
            f" {args.session_id!r} — nothing enabled (the session's open directory is unknown)",
            file=sys.stderr,
        )
        return 2
    already = set(settings_local_plugin_ids(open_path))
    newly_enabled: list[str] = []

    for raw_path in args.paths:
        for plugin_id in matching_plugin_ids(Path(raw_path), open_path=open_path):
            if plugin_id in already or plugin_id in newly_enabled:
                continue
            result = enable_plugin_local(plugin_id, cwd=open_path)
            if result.success:
                newly_enabled.append(plugin_id)
            elif result.already_enabled:
                # déjà actif au scope local : rien à faire, on le note.
                already.add(plugin_id)
                print(
                    f"activate_plugins_for_repos: {plugin_id} already enabled at local scope"
                    " — skipped",
                    file=sys.stderr,
                )
            else:
                print(
                    f"activate_plugins_for_repos: failed to enable {plugin_id}: {result.stderr}",
                    file=sys.stderr,
                )

    for plugin_id in newly_enabled:
        print(plugin_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
