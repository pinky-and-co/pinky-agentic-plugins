#!/usr/bin/env python3
"""Answer-sanitizing policy for every pool model's reply — reasoning out, final content in.

A reasoning model's working notes must never enter a Claude Code conversation as a tool result:
Anthropic's server-side safeguards refuse a turn whose context carries another model's hidden
reasoning (`[reasoning_extraction]`), killing the session. Measured on DeepSeek V4: the reasoning
arrives inline in the assistant text block itself — a leading deliberation, then a stray
`</think>` closer (the opener never survives the provider), then the final report.
No pi flag stops it (`thinking: off` is already those entries' level and changes nothing), so the
cut happens here, on the assembled answer, at `call_cli`'s single choke point (`adversarial`,
`panel`, `pool_check` and the CLI all pass through it).

Policy, in order:

1. Paired blocks (`<think>…</think>`, `|RF-thinking|…||`) are removed wherever they sit.
2. A stray `</think>` with no opener: everything up to the last one is reasoning — only the tail
   after it is kept (a lone `||` never cuts anything: it also ends markdown table rows).
3. An unterminated `<think>` opener: the rest of the text is reasoning. A lone `|RF-thinking|`
   token loses its marker only.
4. A leading deliberation before the first structural marker (heading, bullet, numbered item) is
   dropped when it reads as working notes ("Let me…", "Now I…"). No structural marker anywhere and
   the text stays untouched; when in doubt, content is kept — a clean answer passes through
   byte-identical.

The full raw output (the whole message, reasoning fields included, plus any non-event lines) is
appended to `raw_log_path()` before anything is cut, so nothing is lost — `PI_INCLUDE_REASONING=1`
restores the verbatim answer for a caller that explicitly wants it.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REASONING_ENV = "PI_INCLUDE_REASONING"

# Blocs de raisonnement délimités, par opener/closer exacts. `||` ne coupe que apparié à son
# opener : isolé, c'est aussi le séparateur de cellules d'un tableau markdown.
_PAIRED_BLOCKS: tuple[tuple[str, str], ...] = (
    ("<think>", "</think>"),
    ("|RF-thinking|", "||"),
)

# Marqueur structurel qui ouvre la réponse finale attendue (titre, puce, item numéroté) — la
# délibération de tête n'est coupée que lorsqu'un de ces marqueurs suit.
_STRUCTURE_RE = re.compile(r"^(?:#{1,6} |- |\* |\d+\. )", re.MULTILINE)

# Notes de travail à la première personne, telles qu'un modèle de raisonnement les écrit avant
# sa réponse. Garde-fou du retrait du préambule : un intro sans aucun de ces tours est conservée.
_DELIBERATION_RE = re.compile(
    r"\b(?:let me|i need to|i should|i will|now i|wait,|hmm|first, i|okay,|alright)\b",
    re.IGNORECASE,
)


def extract_answer(message: dict[str, Any]) -> str:
    """The answer text of an assistant `message_end` message — its `text` content blocks only.

    Separation at the source: a reasoning block (`thinking`, `redacted_thinking`), a tool call or
    a top-level reasoning field (`reasoning_content`) never reaches the answer, whatever a
    provider ships alongside the text.

    Args:
        message: the last assistant message of a `pi --mode json` run.

    Returns:
        The `text` blocks joined in order; empty when the message carries none.
    """
    return "".join(
        str(block.get("text", ""))
        for block in message.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _remove_paired_blocks(text: str) -> str:
    """`text` minus every opener/closer-delimited reasoning block, wherever it sits."""
    for opener, closer in _PAIRED_BLOCKS:
        text = re.sub(
            re.escape(opener) + r".*?" + re.escape(closer), "", text, flags=re.DOTALL
        )
    return text


def _cut_before_stray_think_close(text: str) -> str:
    """`text` keeping only what follows the last `</think>` that has no opener left.

    The measured DeepSeek shape: the provider eats the opening `<think>` but not the closing tag,
    so everything before that stray closer is reasoning and the final answer is the tail.
    """
    strays = list(re.finditer(re.escape("</think>"), text))
    if not strays:
        return text
    return text[strays[-1].end() :].lstrip("\n")


def _cut_from_unterminated_think_opener(text: str) -> str:
    """`text` cut at the first `<think>` that never closes — the rest is reasoning.

    A lone `|RF-thinking|` opener is less decisive: only its marker goes, the text stays.
    """
    opener = re.search(re.escape("<think>"), text)
    if opener is not None:
        return text[: opener.start()]
    return text.replace("|RF-thinking|", "")


def _drop_leading_deliberation(text: str) -> str:
    """`text` without its leading working notes, when a structural marker opens the real answer.

    The preamble goes only when both hold: a structural marker follows it (without one, nothing
    distinguishes deliberation from a prose answer — everything stays), and it reads as working
    notes (`_DELIBERATION_RE`). A preamble with none of those turns is a real introduction and
    stays.
    """
    marker = _STRUCTURE_RE.search(text)
    if marker is None or marker.start() == 0:
        return text
    if _DELIBERATION_RE.search(text[: marker.start()]):
        return text[marker.start() :]
    return text


def sanitize_answer(text: str) -> str:
    """A pool model's answer with every reasoning trace removed (see the module policy).

    A text without reasoning markers passes through byte-identical.

    Args:
        text: the assembled text blocks of the model's last assistant message.

    Returns:
        The final-answer text; empty when the whole text was reasoning.
    """
    if not text:
        return text
    cleaned = _remove_paired_blocks(text)
    cleaned = _cut_before_stray_think_close(cleaned)
    cleaned = _cut_from_unterminated_think_opener(cleaned)
    return _drop_leading_deliberation(cleaned)


def raw_log_path() -> Path:
    """This session's raw-model-output log path — the full unsanitized message, one JSON line per
    call, under `~/.claude/debug/` beside the pool's own debug log.

    Keyed by `CLAUDE_CODE_SESSION_ID` (`standalone` when unset), same convention as
    `model_adversarial._pool_log_path`: durable enough for a human or a follow-up tool call to
    read what the model actually said, without any of it entering the conversation.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "standalone")
    return (
        Path.home()
        / ".claude"
        / "debug"
        / f"{session_id}_mcp__pc-core__model-raw.jsonl"
    )


def append_raw(
    entry: dict[str, Any], message: dict[str, Any], stray: list[str]
) -> Path | None:
    """Best-effort append of one raw answer record to `raw_log_path()`.

    Swallows any failure to write (missing directory and unable to create it, permission issue,
    full disk) — a logging failure must never break a call that otherwise answered. The record
    keeps everything the sanitizer will cut: the whole message (reasoning fields and thinking
    blocks included) and the run's non-event lines.

    Args:
        entry: the pool entry that answered — its `name` and `model` stamp the record.
        message: the last assistant message, verbatim.
        stray: the run's stdout lines that were not events.

    Returns:
        The path written to, or None when the write failed.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "entry": entry.get("name"),
        "model": entry.get("model"),
        "message": message,
        "stray": stray,
    }
    try:
        path = raw_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
        return path
    except (OSError, RuntimeError, ValueError):
        return None


def include_reasoning() -> bool:
    """Whether the caller asked for the verbatim answer — `PI_INCLUDE_REASONING=1`."""
    return os.environ.get(REASONING_ENV) == "1"
