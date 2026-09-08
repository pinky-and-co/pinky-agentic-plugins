#!/usr/bin/env python3
"""sync_plugin_scope — activates the right plugins for wherever the session
actually is, without ever touching a .claude/settings.json source file.

Ascend at SessionStart, descend at CwdChanged, restore at SessionEnd:

- SessionStart: back up the session's single settings.local.json (resolved
  from CLAUDE_PROJECT_DIR, same value inject_policies.py already reads — see
  plugin_scope_lib's module docstring for why it's one file, not one per
  repo touched), then activate every project-scoped plugin whose projectPath
  is an ancestor of (or equal to) the session's starting cwd.
- CwdChanged: same matching, against the *new* cwd, additive only — never
  deactivates a plugin a previous position already turned on.
- SessionEnd: restore settings.local.json to exactly what backup_settings_local
  captured, and drop the per-session state file.

Registered as a single script across three hook events in hooks.json (see
hooks.json.snippet.json) rather than three files — one entry to maintain,
dispatch on `hook_event_name` from stdin.

No attempt is made to run /reload-plugins from here — hooks have no
extension point for it. See notify_reload.py for the FileChanged-driven
reminder instead.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from notify_helpers import notify
from plugin_scope_lib import (
    backup_settings_local,
    clear_state,
    enable_plugin_local,
    load_state,
    matching_plugin_ids,
    restore_settings_local,
    save_state,
)


def settings_local_for(project_dir: str) -> Path:
    return Path(project_dir) / ".claude" / "settings.local.json"


def activate_for_path(path: Path, state: dict) -> list[str]:
    """Enable every not-yet-activated matching plugin for `path`. Returns the
    plugin ids newly enabled this call (empty if nothing new — cheap to call
    on every CwdChanged, most of which won't match anything new)."""
    already = set(state["activated"])
    newly_enabled = []
    for plugin_id in matching_plugin_ids(path):
        if plugin_id in already:
            continue
        result = enable_plugin_local(plugin_id)
        if result.success:
            already.add(plugin_id)
            newly_enabled.append(plugin_id)
        else:
            print(
                f"sync_plugin_scope: failed to enable {plugin_id}: {result.stderr}",
                file=sys.stderr,
            )
    state["activated"] = sorted(already)
    return newly_enabled


def handle_session_start(payload: dict) -> None:
    session_id = payload["session_id"]
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", payload.get("cwd", os.getcwd()))
    settings_local = settings_local_for(project_dir)

    state = load_state(session_id)
    state["settings_local_path"] = str(settings_local)
    state["existed"] = backup_settings_local(settings_local, session_id)

    newly_enabled = activate_for_path(Path(payload.get("cwd", project_dir)), state)
    save_state(session_id, state)

    output: dict = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
        }
    }
    if newly_enabled:
        message = (
            f"{len(newly_enabled)} new plugin{'s' if len(newly_enabled) > 1 else ''} activated!"
            + "\n🚀 Run /reload-plugins  for the change to take effect in this session."
        )
        output.update(notify("Claude Code", message))

    print(json.dumps(output))


def handle_cwd_changed(payload: dict) -> None:
    session_id = payload["session_id"]
    new_cwd = payload.get("cwd")
    if not new_cwd:
        return  # defensive — cwd is a common field but guard anyway

    state = load_state(session_id)
    newly_enabled = activate_for_path(Path(new_cwd), state)
    save_state(session_id, state)

    if newly_enabled:
        # CwdChanged has no additionalContext / decision control (context-only
        # events per the hooks reference) — systemMessage is the visible channel,
        # notify() also handles desktop-notification delivery across terminals.
        message = (
            f"{len(newly_enabled)} new plugin{'s' if len(newly_enabled) > 1 else ''} activated!"
            + "\n🚀 Run /reload-plugins  for the change to take effect in this session."
        )
        output: dict = notify("Claude Code", message)
        print(json.dumps(output))


def handle_session_end(payload: dict) -> None:
    session_id = payload["session_id"]
    state = load_state(session_id)
    settings_local_path = state.get("settings_local_path")
    if settings_local_path:
        restore_settings_local(
            Path(settings_local_path), session_id, state.get("existed")
        )
    clear_state(session_id)


def main() -> None:
    payload = json.load(sys.stdin)
    event = payload.get("hook_event_name")

    if event == "SessionStart":
        handle_session_start(payload)
    elif event == "CwdChanged":
        handle_cwd_changed(payload)
    elif event == "SessionEnd":
        handle_session_end(payload)
    # any other event: silently do nothing (script shouldn't be wired to it)


if __name__ == "__main__":
    main()
