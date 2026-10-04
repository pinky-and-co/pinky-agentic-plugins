#!/usr/bin/env python3
"""
subagentStatusLine — une ligne personnalisée par sous-agent affiché dans le
panneau d'agents de Claude Code. Reçoit un JSON unique sur stdin avec tous
les agents visibles (champ "tasks"), et doit écrire UNE ligne JSON par
agent que l'on veut personnaliser : {"id": "<task id>", "content": "<rendu>"}
Un id absent du flux de sortie garde le rendu par défaut de Claude Code.
"""

import json
import sys
from pathlib import Path

from _common import (
    BLUE,
    CYAN,
    DIM,
    GREEN,
    GREY,
    MAGENTA,
    RED,
    RESET,
    YELLOW,
    enable_ansi_on_windows,
    force_utf8_stdout,
    render_bar,
)

enable_ansi_on_windows()
force_utf8_stdout()

SEP = f"{GREY} · {RESET}"

STATUS_EMOJI = {
    "running": "⚙️",
    "in_progress": "⚙️",
    "pending": "⏳",
    "queued": "⏳",
    "completed": "✅",
    "complete": "✅",
    "success": "✅",
    "failed": "❌",
    "error": "❌",
}


def _color_for_pct(pct):
    if pct >= 90:
        return RED
    if pct >= 70:
        return YELLOW
    return GREEN


def _mini_bar(pct, width=5):
    return render_bar(pct, width=width, color=_color_for_pct(pct))


def _short_model(model_id):
    """claude-opus-4-8 -> Opus 4.8, claude-sonnet-5 -> Sonnet 5, etc."""
    if not model_id:
        return "?"
    name = model_id.replace("claude-", "")
    parts = name.split("-")
    if not parts:
        return model_id
    label = parts[0].capitalize()
    version_parts = [p for p in parts[1:] if not (p.isdigit() and len(p) >= 6)]
    version = ".".join(version_parts)
    return f"{label} {version}".strip()


def _visible_len(s):
    """Longueur du texte sans compter les codes ANSI, pour le calcul de troncature."""
    out, in_escape = 0, False
    i = 0
    while i < len(s):
        if s[i] == "\033":
            in_escape = True
        elif in_escape and s[i] == "m":
            in_escape = False
        elif not in_escape:
            out += 1
        i += 1
    return out


def _agent_type(transcript_path: str | None, task_id: str) -> str | None:
    """`agentType` of a subagent, read from `<transcript>/subagents/agent-<id>.meta.json`, or
    None when that file is missing or unreadable (layout observed, not a documented contract)."""
    if not transcript_path:
        return None
    meta = (
        Path(transcript_path).with_suffix("")
        / "subagents"
        / f"agent-{task_id}.meta.json"
    )
    try:
        return json.loads(meta.read_text(encoding="utf-8")).get("agentType")
    except (OSError, ValueError, AttributeError):
        return None


def render_task(task: dict, columns: int, agent_type: str | None = None) -> dict | None:
    """Build the `{"id": ..., "content": ...}` line for one task, or None to skip it (an id
    absent from the output falls back to Claude Code's default rendering for that row — the
    schema's own graceful-degradation path, reused here instead of inventing another one)."""
    task_id = task.get("id")
    if not task_id:
        return None

    name = task.get("name") or task.get("label") or task.get("type") or "agent"
    subagent = agent_type or (
        (str(task.get("description")).split(" ")[0]).split(":")[-1].replace("/", "")
    )
    if name == subagent:
        subagent = ""
    else:
        subagent = f"{SEP}{MAGENTA}🧬 {subagent}"
    status = (task.get("status") or "").lower()
    status_emoji = (
        status.capitalize().replace("-", " ") + " " + STATUS_EMOJI.get(status, "▪️")
    )

    model = _short_model(task.get("model"))

    effort = task.get("effort")
    effort_txt = f"💪 : {effort}" if effort else f"{DIM}💪 : —{RESET}"

    token_count = task.get("tokenCount")
    ctx_size = task.get("contextWindowSize")
    if (
        isinstance(token_count, (int, float))
        and isinstance(ctx_size, (int, float))
        and ctx_size
    ):
        pct = int(token_count / ctx_size * 100)
        ctx_txt = f"{_mini_bar(pct)} {_color_for_pct(pct)}{pct}%{RESET}"
    else:
        ctx_txt = f"{DIM}ctx:—{RESET}"

    suffix = f"{SEP}{CYAN}🤖 {model}{RESET}{SEP}{effort_txt}{SEP}{ctx_txt}"

    fixed_len = _visible_len(status_emoji) + 1 + _visible_len(suffix)

    name_budget = max(6, columns - fixed_len)
    if len(name) > name_budget:
        name = name[: max(1, name_budget - 1)] + "…"

    content = f"{status_emoji} {BLUE}{name}{subagent}{RESET}{suffix}"
    return {"id": task_id, "content": content}


def main():
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return
    if not isinstance(data, dict):
        print(
            json.dumps(
                {"error": f"top-level JSON is {type(data).__name__}, not object"}
            ),
            file=sys.stderr,
        )
        return

    columns = data.get("columns", 80) or 80
    tasks = data.get("tasks", []) or []
    if not isinstance(tasks, list):
        tasks = []

    if not tasks:
        print(json.dumps({"debug": "no tasks"}), file=sys.stderr)

    for task in tasks:
        if not isinstance(task, dict):
            continue
        try:
            line = render_task(
                task,
                columns,
                _agent_type(data.get("transcript_path"), str(task.get("id"))),
            )
        except Exception as e:
            print(
                json.dumps({"error": str(e), "task_id": task.get("id")}),
                file=sys.stderr,
            )
            continue
        if line is not None:
            print(json.dumps(line))


if __name__ == "__main__":
    main()
