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
  deactivates a plugin a previous position already turned on. Passes the
  session's open path so the match drops what the SessionStart ascend
  already covered (projectPath at or above the open dir) and what
  settings.local.json already lists.
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
    settings_local_plugin_ids,
)


def settings_local_for(project_dir: str) -> Path:
    return Path(project_dir) / ".claude" / "settings.local.json"


def session_open_path(state: dict) -> Path | None:
    """The directory the session opened in — the one `claude plugin enable -s
    local` resolves against. Recovered from the settings.local.json path
    handle_session_start already stored (`<open>/.claude/settings.local.json`),
    falling back to CLAUDE_PROJECT_DIR when that run left nothing (it always
    runs first, so this is belt-and-braces)."""
    stored = state.get("settings_local_path")
    if stored:
        return Path(stored).parent.parent
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env) if env else None


def activate_for_path(
    path: Path, state: dict, *, open_path: Path | None = None
) -> list[str]:
    """Enable every not-yet-activated matching plugin for `path`. Returns the
    plugin ids newly enabled this call (empty if nothing new — cheap to call
    on every CwdChanged, most of which won't match anything new).

    `open_path` is the session's open directory (the CwdChanged descent
    passes it, the SessionStart ascend does not): it narrows the match to
    plugins genuinely below the session root and seeds the skip-set with
    what settings.local.json already lists, so an id already on is never
    handed to `claude plugin enable` a second time.
    """
    already = set(state["activated"])
    if open_path is not None:
        already.update(settings_local_plugin_ids(open_path))
    newly_enabled = []
    for plugin_id in matching_plugin_ids(path, open_path=open_path):
        if plugin_id in already:
            continue
        result = enable_plugin_local(plugin_id)
        if result.success:
            already.add(plugin_id)
            newly_enabled.append(plugin_id)
        elif result.already_enabled:
            # déjà actif au scope local : rien à faire, on le note pour ne pas
            # ré-essayer (et ré-avertir) au prochain cd.
            already.add(plugin_id)
            print(
                f"sync_plugin_scope: {plugin_id} already enabled at local scope — skipped",
                file=sys.stderr,
            )
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
    newly_enabled = activate_for_path(
        Path(new_cwd), state, open_path=session_open_path(state)
    )
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
