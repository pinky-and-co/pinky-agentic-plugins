#!/usr/bin/env python3
"""SessionEnd hook — write the cross-agent log entry a session that did real work never wrote.

A session ends when nothing can make its own model act any more, so this hook hands the entry to a
detached headless writer (`claude -p`, haiku, restricted to Read/Glob/Write inside `_AGENT_LOG/`)
fed a condensed excerpt of the transcript. It launches only when both hold, checked by a pure scan
of the transcript (no model call otherwise):

- the session did real work — a commit (Bash `git commit`, a commit skill, the git-workflow agent)
  or an edit/move under `_KANBAN/`;
- the session wrote nothing under `_AGENT_LOG/`.

The writer decides whether anything is worth logging (skill `kanban` § Cross-agent log) and may
write nothing. The excerpt keeps user messages, assistant text, edited paths and commit commands —
never tool outputs — and goes through `scrub` before leaving the machine.

Python 3.11+, stdlib only. Exit code is always 0 (a hook failure must never block a SessionEnd).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent))
from pc_kanban_secret_patterns import scrub  # noqa: E402
from stop_handoff import agent_log_dir, find_kanbans, read_event  # noqa: E402

GUARD_ENV = "PC_KANBAN_AGENT_LOG_WRITER"
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
MAX_BLOCK_CHARS = 1500
MAX_EXCERPT_CHARS = 60_000
WRITER_MODEL = "haiku"

_GIT_COMMIT = re.compile(r"\bgit\b[^|;&\n]*\bcommit\b")
_SHELL_WRITE = re.compile(r"(>|\btee\b|\bmv\b|\bcp\b)")
_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


class Scan(NamedTuple):
    """What the transcript shows: real work, an agent-log write, and the excerpt lines."""

    did_work: bool
    wrote_log: bool
    lines: list[str]


def _touches(path: str, marker: str) -> bool:
    """True when `path` lies under a `marker` directory (`_KANBAN`, `_AGENT_LOG`)."""
    return f"/{marker}/" in f"/{path}"


def _clip(text: str) -> str:
    """One excerpt block, bounded."""
    text = text.strip()
    return text if len(text) <= MAX_BLOCK_CHARS else text[:MAX_BLOCK_CHARS] + " […]"


def _entries(transcript: Path) -> list[dict]:
    """The transcript's user/assistant entries; unreadable lines skipped."""
    out: list[dict] = []
    try:
        raw = transcript.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("type") in ("user", "assistant"):
            out.append(entry)
    return out


def scan_transcript(transcript: Path) -> Scan:
    """Classify the session and build its excerpt in one pass over the transcript."""
    did_work = wrote_log = False
    lines: list[str] = []
    for entry in _entries(transcript):
        if entry.get("isMeta"):
            continue
        role = "USER" if entry["type"] == "user" else "ASSISTANT"
        content = (entry.get("message") or {}).get("content")
        blocks = (
            [{"type": "text", "text": content}] if isinstance(content, str) else content
        )
        for block in blocks or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = _SYSTEM_REMINDER.sub("", block.get("text") or "")
                if text.strip() and not text.lstrip().startswith("<local-command"):
                    lines.append(f"{role}: {_clip(text)}")
            elif block.get("type") == "tool_use":
                name = block.get("name") or ""
                raw_input = block.get("input")
                inp: dict = raw_input if isinstance(raw_input, dict) else {}
                if name in WRITE_TOOLS:
                    path = str(inp.get("file_path") or inp.get("notebook_path") or "")
                    lines.append(f"EDITED: {path}")
                    did_work |= _touches(path, "_KANBAN")
                    wrote_log |= _touches(path, "_AGENT_LOG")
                elif name == "Bash":
                    cmd = str(inp.get("command") or "")
                    if _GIT_COMMIT.search(cmd):
                        did_work = True
                        lines.append(f"COMMIT: {_clip(cmd)}")
                    if "_KANBAN/" in cmd and re.search(r"\bmv\b", cmd):
                        did_work = True
                    if "_AGENT_LOG/" in cmd and _SHELL_WRITE.search(cmd):
                        wrote_log = True
                elif name == "Skill":
                    skill = str(inp.get("skill") or "")
                    did_work |= skill.endswith((":commit", ":commit-and-pr"))
                elif name == "Agent":
                    did_work |= str(inp.get("subagent_type") or "").endswith(
                        "git-workflow"
                    )
    return Scan(did_work, wrote_log, lines)


def build_excerpt(lines: list[str]) -> str:
    """Scrubbed excerpt, capped from the end — the close of a session carries its conclusions."""
    text = scrub("\n".join(lines))
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    return "[…]\n" + text[-MAX_EXCERPT_CHARS:]


def pick_log_dir(cwd: Path, excerpt: str) -> Path | None:
    """The agent-log dir of the board this session worked on: the mounted board whose root the
    excerpt names most, else the nearest one."""
    kanbans = find_kanbans(cwd)
    if not kanbans:
        return None
    best = max(kanbans, key=lambda k: excerpt.count(f"{k.parent}/"))
    return agent_log_dir(best)


def tool_name() -> str | None:
    """The running agentic tool's own name, from its environment (`claude-code_2-1-261_agent` →
    `claude-code`); None when the tool exposes none — never a hardcoded default."""
    raw = os.environ.get("AI_AGENT", "").strip()
    return raw.split("_", 1)[0] or None


def git_identity(cwd: Path) -> str | None:
    """`git config user.name`, the `<agent-identity>` of a log filename."""
    try:
        out = subprocess.run(  # nosec B603 B607 — argv fixe, aucune entrée externe
            ["git", "config", "user.name"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def build_prompt(excerpt: str, today: str, tool: str, identity: str) -> str:
    """The writer's whole instruction set — it inherits nothing else."""
    prefix = f"{today}_{tool}-{identity}"
    return f"""You write one entry in a cross-agent log — your working directory — for a coding
session that just ended. The log is the async narrative channel between sessions: what a session
did, across chantiers, that no single kanban card's handoff would hold — decisions, root causes
found, what was left open, gotchas another session would otherwise rediscover.

Decide first. If the excerpt holds nothing another session needs (a routine fix fully told by its
commit, exploration with no conclusion), write nothing and reply `nothing to log`.

Otherwise write exactly one new file `{prefix}_<slug>.md`, <slug> being 3-6 kebab-case words naming
the subject. Read today's existing `{prefix}*.md` first: never repeat what one already says, never
modify one. First line `{today} · {tool} ({identity})`, then `## ` sections, dense prose, file
paths and ids exact. Write in the language of the excerpt's `USER:` lines, whatever language this
instruction is in. Never include secrets, credentials or internal URLs.

Session excerpt — user messages, assistant text, edited paths, commit commands; tool outputs
omitted:
<excerpt>
{excerpt}
</excerpt>
"""


def launch_writer(log_dir: Path, prompt: str) -> bool:
    """Start the headless writer detached from this session; False when it cannot start."""
    claude = shutil.which("claude")
    if not claude:
        return False
    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", prefix="pc-kanban-agent-log-", delete=False
    ) as fh:
        fh.write(prompt)
    log = Path(tempfile.gettempdir()) / "pc-kanban-agent-log-writer.log"
    cmd = [
        claude,
        "-p",
        "--model",
        WRITER_MODEL,
        "--restricted",
        "--tools",
        "Read,Glob,Write",
        "--permission-mode",
        "acceptEdits",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--disable-slash-commands",
    ]
    try:
        with open(fh.name, encoding="utf-8") as stdin, open(log, "a") as out:
            subprocess.Popen(  # nosec B603 — argv fixe, binaire résolu par shutil.which
                cmd,
                cwd=log_dir,
                stdin=stdin,
                stdout=out,
                stderr=out,
                env={**os.environ, GUARD_ENV: "1"},
                start_new_session=True,
            )
    except OSError:
        return False
    return True


def main() -> None:
    """Hook entry point."""
    if os.environ.get(GUARD_ENV):
        return
    event = read_event()
    transcript = event.get("transcript_path")
    if not transcript:
        return
    scan = scan_transcript(Path(transcript))
    if not scan.did_work or scan.wrote_log:
        return
    cwd = Path(event.get("cwd") or os.getcwd())
    excerpt = build_excerpt(scan.lines)
    log_dir = pick_log_dir(cwd, excerpt)
    tool, identity = tool_name(), git_identity(cwd)
    if log_dir is None or not log_dir.is_dir() or not tool or not identity:
        return
    launch_writer(
        log_dir, build_prompt(excerpt, date.today().isoformat(), tool, identity)
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001  # nosec B110 — un hook SessionEnd ne fait jamais échouer la sortie
        pass
    sys.exit(0)
