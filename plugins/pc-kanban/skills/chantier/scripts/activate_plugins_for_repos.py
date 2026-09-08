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

"Already enabled" is checked against enabledPlugins in the session's actual
settings.local.json, not a separate session-state file: that's the ground
truth Claude Code itself reads, and it's what sync_plugin_scope.py's own
CwdChanged calls may have already changed earlier in this same session —
reading the real file, rather than shadow state, is what keeps the two
mechanisms from disagreeing.

Prints one plugin id per line for everything newly enabled this call, empty
stdout if nothing changed — that's the signal SKILL.md's Repo-scope bullet
reads to decide whether the /reload-plugins interrupt is needed. A per-id
enable failure is reported on stderr and skipped, never aborts the rest —
same posture as sync_plugin_scope.py's own activate_for_path().

Usage: activate_plugins_for_repos.py <resolved-repo-path> [<path>...]
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from plugin_scope_lib import enable_plugin_local, matching_plugin_ids


def settings_local_path() -> Path:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    return Path(project_dir) / ".claude" / "settings.local.json"


def currently_enabled() -> set[str]:
    """Plugin ids already in enabledPlugins for this session, or an empty
    set if settings.local.json is absent/unreadable — same tolerant,
    read-only posture as plugin_scope_lib.load_installed_plugins().

    ASSUMPTION not yet verified against a real settings.local.json: that
    enabledPlugins is a dict keyed by the same "plugin@marketplace" id
    format matching_plugin_ids() returns. Worth a quick check against an
    actual file before trusting this dedup blindly.
    """
    try:
        data = json.loads(settings_local_path().read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return set(data.get("enabledPlugins", {}).keys())


def main() -> int:
    paths = sys.argv[1:]
    if not paths:
        print(
            "usage: activate_plugins_for_repos.py <resolved-repo-path>...",
            file=sys.stderr,
        )
        return 2

    already = currently_enabled()
    newly_enabled: list[str] = []

    for raw_path in paths:
        for plugin_id in matching_plugin_ids(Path(raw_path)):
            if plugin_id in already or plugin_id in newly_enabled:
                continue
            result = enable_plugin_local(plugin_id)
            if result.success:
                newly_enabled.append(plugin_id)
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
