#!/usr/bin/env python3
"""sync_plugin_scope — activates the right plugins for wherever the session
actually is.

Ascend at SessionStart, descend at CwdChanged, restore at SessionEnd:

- SessionStart: back up the session's single settings.local.json (resolved
  from CLAUDE_PROJECT_DIR, same value inject_policies.py already reads — see
  plugin_scope_lib's module docstring for why it's one file, not one per
  repo touched), then activate — at project scope, in the repo opened in —
  every project-scoped plugin whose projectPath is an ancestor of it. Same
  call `spawn-workers`' pre-enable makes (`claude plugin install --scope
  project`), so the write lands in that repo's versioned `.claude/
  settings.json`, not an ephemeral settings.local.json.
- CwdChanged: local-scope matching against the *new* cwd, additive only —
  never deactivates a plugin a previous position already turned on. Passes
  the session's open path so the match drops what the SessionStart ascend
  already covered (projectPath at or above the open dir) and what
  settings.local.json already lists.
- SessionEnd: restore settings.local.json to exactly what backup_settings_local
  captured, drop the per-session state file, and clear the pending queue —
  never touches the ascend's project-scope settings.json, which stays.

Registered as a single script across three hook events in hooks.json rather
than three files — one entry to maintain, dispatch on `hook_event_name`
from stdin.

Newly-activated plugins need `/reload-plugins`, which no hook can run. The
request goes into pending_user_action's queue; pending_gate turns the next
tool call into a permission `ask` carrying it, so it can't scroll away.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pending_user_action import clear_all as clear_pending
from pending_user_action import require
from plugin_scope_lib import (
    backup_settings_local,
    clear_state,
    enable_plugin_local,
    install_plugin_project,
    load_state,
    matching_plugin_ids,
    project_scope_installed_ids,
    restore_settings_local,
    save_state,
    settings_json_plugin_ids,
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
    path: Path, state: dict, *, session_dir: Path, descent: bool
) -> list[str]:
    """Enable every not-yet-activated matching plugin for `path`. Returns the
    plugin ids newly enabled this call (empty if nothing new — cheap to call
    on every CwdChanged, most of which won't match anything new).

    `session_dir` is the directory the session opened in. `descent=True`
    (CwdChanged) enables at local scope (`claude plugin enable -s local`,
    always run from `session_dir` so every write lands in that one
    settings.local.json) and additionally drops plugins whose projectPath is
    at or above `session_dir` — the SessionStart ascend already handled
    those. `descent=False` (the ascend, `session_dir` == the repo opened in)
    enables at project scope instead (`claude plugin install --scope
    project`, same call `spawn-workers` makes) — the write is then versioned
    in `session_dir/.claude/settings.json`, not ephemeral.
    """
    if descent:
        already = set(state["activated"]) | set(settings_local_plugin_ids(session_dir))
        exclude_above: Path | None = session_dir
    else:
        already = (
            set(state["activated"])
            | set(settings_json_plugin_ids(session_dir))
            | set(project_scope_installed_ids(session_dir))
        )
        exclude_above = None
    newly_enabled = []
    for plugin_id in matching_plugin_ids(path, open_path=exclude_above):
        if plugin_id in already:
            continue
        result = (
            enable_plugin_local(plugin_id, cwd=session_dir)
            if descent
            else install_plugin_project(plugin_id, cwd=session_dir)
        )
        if result.success:
            already.add(plugin_id)
            newly_enabled.append(plugin_id)
        elif result.already_enabled:
            # déjà actif au scope visé : rien à faire, on le note pour ne pas
            # ré-essayer (et ré-avertir) au prochain passage.
            already.add(plugin_id)
            print(
                f"sync_plugin_scope: {plugin_id} already enabled — skipped",
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

    newly_enabled = activate_for_path(
        Path(payload.get("cwd", project_dir)),
        state,
        session_dir=Path(project_dir),
        descent=False,
    )
    save_state(session_id, state)

    if newly_enabled:
        require(session_id, "reload-plugins", _reload_message(newly_enabled))

    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart"}}))


def _reload_message(newly_enabled: list[str]) -> str:
    n = len(newly_enabled)
    return (
        f"/reload-plugins  — {n} plugin{'s' if n > 1 else ''} "
        f"activé{'s' if n > 1 else ''} ({', '.join(newly_enabled)}), "
        "à charger pour cette session"
    )


def handle_cwd_changed(payload: dict) -> None:
    session_id = payload["session_id"]
    new_cwd = payload.get("cwd")
    if not new_cwd:
        return  # defensive — cwd is a common field but guard anyway

    state = load_state(session_id)
    session_dir = session_open_path(state)
    if session_dir is None:
        return
    newly_enabled = activate_for_path(
        Path(new_cwd), state, session_dir=session_dir, descent=True
    )
    save_state(session_id, state)

    if newly_enabled:
        # CwdChanged est context-only (aucun contrôle de décision) : on met la
        # demande dans la file pending_user_action, que pending_gate transforme
        # en `ask` bloquant au prochain tool call — pas une notif éphémère.
        require(session_id, "reload-plugins", _reload_message(newly_enabled))


def handle_session_end(payload: dict) -> None:
    session_id = payload["session_id"]
    state = load_state(session_id)
    settings_local_path = state.get("settings_local_path")
    if settings_local_path:
        restore_settings_local(
            Path(settings_local_path), session_id, state.get("existed")
        )
    clear_state(session_id)
    clear_pending(session_id)


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
