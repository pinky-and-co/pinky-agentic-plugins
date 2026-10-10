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
import time
import unicodedata
from pathlib import Path

from _common import (
    BLUE,
    CYAN,
    DIM,
    GREY,
    MAGENTA,
    RESET,
    enable_ansi_on_windows,
    force_utf8_stdout,
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

RUNNING_STATUSES = {"running", "in_progress"}

SPARKS = "▁▂▃▄▅▆▇█"

# Le nom n'est jamais tronqué en dessous de cette largeur
_MIN_NAME_COLS = 6


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


def _format_duration(start_ms, now_ms=None):
    """Elapsed time as `42s`, `1m42s` or `1h05m`; None if start_ms is not a number."""
    if not isinstance(start_ms, (int, float)):
        return None
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    seconds = max(
        0, int((now_ms - start_ms) // 1000)
    )  # max : tolère un léger décalage d'horloge
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _sparkline(samples):
    """Activity sparkline: one block per interval, height = tokens generated since the previous sample."""
    if not isinstance(samples, list):
        return None
    nums = [s for s in samples if isinstance(s, (int, float))]
    if len(nums) < 2:
        return None
    # delta négatif = nouveau tour (le compteur est repassé à zéro) : on repart de la valeur
    deltas = [cur - prev if cur >= prev else cur for prev, cur in zip(nums, nums[1:])]
    peak = max(deltas)
    if peak <= 0:
        return SPARKS[0] * len(deltas)
    return "".join(SPARKS[min(7, int(d / peak * 7))] for d in deltas)


def _visible_len(s: str) -> int:
    """Terminal width of `s`: ANSI colour codes and OSC 8 hyperlinks count for nothing, wide
    characters (emojis, CJK) for two columns. Mirrors `statusline.py::_visible_len`."""
    WIDE_RANGES = (
        (0x1100, 0x115F),
        (0x2300, 0x23FF),
        (0x2600, 0x27BF),
        (0x2E80, 0xA4CF),
        (0xAC00, 0xD7A3),
        (0xF900, 0xFAFF),
        (0xFF00, 0xFF60),
        (0xFFE0, 0xFFE6),
        (0x1F194, 0x1F194),
        (0x1F1E6, 0x1F1FF),
        (0x1F300, 0x1FAFF),
    )
    ZERO_WIDTH_CODEPOINTS = {0x200D, 0xFE0E, 0xFE0F}

    out = 0
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\033":
            nxt = s[i + 1] if i + 1 < n else ""
            if nxt == "[":
                j = i + 2
                while j < n and s[j] != "m":
                    j += 1
                i = j + 1
            elif nxt == "]":
                j = i + 2
                while (
                    j < n
                    and s[j] != "\x07"
                    and not (s[j] == "\033" and s[j + 1 : j + 2] == "\\")
                ):
                    j += 1
                i = j + 1 if (j < n and s[j] == "\x07") else j + 2
            else:
                i += 1
            continue

        cp = ord(ch)
        if cp in ZERO_WIDTH_CODEPOINTS or unicodedata.combining(ch):
            i += 1
            continue
        if any(lo <= cp <= hi for lo, hi in WIDE_RANGES):
            out += 2
        else:
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
    schema's own graceful-degradation path, reused here instead of inventing another one).

    The row reads `<status> <name> · 🧬 <agent type> · 🤖 <model> · 💪 : <effort> · ⏱ <duration>`.
    When it does not fit `columns`, the agent type goes first, then the effort; only then is the
    name truncated (never under `_MIN_NAME_COLS`). Status, model and duration always stay."""
    task_id = task.get("id")
    if not task_id:
        return None

    name = task.get("name") or task.get("label") or task.get("type") or "agent"
    status = (task.get("status") or "").lower()
    status_emoji = (
        status.capitalize().replace("-", " ") + " " + STATUS_EMOJI.get(status, "▪️")
    )

    model = _short_model(task.get("model"))

    effort = task.get("effort")
    effort_txt = f"💪 : {effort}" if effort else f"{DIM}💪 : —{RESET}"

    # Durée écoulée + activité, seulement tant que la tâche tourne (pas de endTime dans le schéma)
    running = status in RUNNING_STATUSES
    duration = _format_duration(task.get("startTime")) if running else None
    spark = _sparkline(task.get("tokenSamples")) if running else None
    duration_txt = f"⏱ {duration}" if duration else f"{DIM}⏱ —{RESET}"
    if duration and spark:
        duration_txt += f" {DIM}{MAGENTA}{spark}{RESET}"

    type_part = f"{SEP}{MAGENTA}🧬 {agent_type}{RESET}" if agent_type else ""
    model_part = f"{SEP}{CYAN}🤖 {model}{RESET}"
    effort_part = f"{SEP}{effort_txt}"
    duration_part = f"{SEP}{duration_txt}"

    # Du plus sacrifiable au moins sacrifiable : le type d'agent, puis l'effort, puis seulement le nom
    tails = (
        type_part + model_part + effort_part + duration_part,
        model_part + effort_part + duration_part,
        model_part + duration_part,
    )
    head_len = _visible_len(status_emoji) + 1
    for tail in tails:
        name_budget = columns - head_len - _visible_len(tail)
        if len(name) <= name_budget:
            break
    # si aucune variante ne tient, `tail` est la plus réduite et le nom est tronqué
    name_budget = max(_MIN_NAME_COLS, name_budget)
    if len(name) > name_budget:
        name = name[: max(1, name_budget - 1)] + "…"

    content = f"{status_emoji} {BLUE}{name}{RESET}{tail}"
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
