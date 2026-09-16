#!/usr/bin/env python3
"""Session-vs-spawn signals for `/chantier`'s "Proposing the next card" step
(`kanban/references/finishing-a-card.md`).

Two independent, best-effort signals, each degrading to `None` rather than raising when its
data source is missing — the caller treats `None` as "omit that part of the line", never as an
error:

- **`context_window_pct`** — rebuilt directly from this session's own transcript
  (`~/.claude/projects/<cwd-with-dashes>/<session_id>.jsonl`). The harness's `context_window`
  payload is only ever pushed to the `statusLine` command, unreachable from a running skill — but
  the ceiling is already sitting in that same transcript as a structured `type: "attachment"`
  entry (`attachment.identity.modelId`, carrying the `[1m]` suffix when it applies), and the
  consumed side comes from the last assistant message's `usage`. No `pc-statusline` dependency.
  Deliberately not a free-text search for the "exact model ID is …" sentence — that sentence is
  plain text the transcript also contains inside ordinary tool/assistant content (a file this
  script's own tests write, a command's echoed output) with no structural marker telling those
  apart from the real reminder; the structured `attachment` entry has no such ambiguity.
- **`evaluate_rate_limit`** — `rate_limits.five_hour`/`.seven_day` are a quota aggregated
  server-side across the whole account, not reconstructible from one session's transcript. The
  only place they land is the same `statusLine` payload, so `pc-statusline` writes them to a
  per-session state file on every render (`statusline.py::_write_rate_limit_state`) and this reads
  it back — the one cross-plugin coupling accepted for this axis, and only this one. A window is
  `triggered` when its usage is running ahead of a uniform pace toward the reset
  (`used_fraction > elapsed_fraction`) *and* the reset is more than 1h away — a reset under 1h is
  short enough that a hit there does not matter.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

CEILING_DEFAULT = 200_000
CEILING_1M = 1_000_000

WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 24 * 3600}

_TAIL_BYTES = 65536
_HEAD_BYTES = 1024 * 1024


def _safe_get(d, path, default=None):
    """Accès sûr à un chemin imbriqué du type 'a.b.c'."""
    cur = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur if cur is not None else default


def _transcript_path(session_id, cwd):
    """Chemin du transcript de cette session — convention Claude Code observée, pas documentée :
    un fichier lu, jamais écrit ici, donc une absence se traite comme n'importe quelle autre
    donnée manquante."""
    if not session_id or not cwd:
        return None
    project_dir = cwd.replace(os.sep, "-")
    return os.path.join(
        os.path.expanduser("~"),
        ".claude",
        "projects",
        project_dir,
        f"{session_id}.jsonl",
    )


def _tail_text(path, tail_bytes=_TAIL_BYTES):
    """Les `tail_bytes` derniers octets du fichier, décodés au mieux — même idiome que
    `statusline.py::_get_advisor_model` pour éviter de reparser un transcript potentiellement
    énorme à chaque appel."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            return f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None


def _head_text(path, head_bytes=_HEAD_BYTES):
    """Les `head_bytes` premiers octets du fichier — l'attachment d'identité de modèle n'est
    journalisé qu'une seule fois, en tout début de session (observé à ~25 Ko sur une vraie
    session), jamais réinjecté en cours de route ; chercher en tail comme pour l'`usage` le
    manquerait dès que la session s'allonge."""
    try:
        with open(path, "rb") as f:
            return f.read(head_bytes).decode("utf-8", errors="ignore")
    except OSError:
        return None


def _model_ceiling_from_transcript(text):
    """Plafond de contexte à partir du dernier `{"type": "attachment", "attachment": {"type":
    "model", "identity": {"modelId": …}}}` du texte — un champ structuré, jamais une recherche de
    texte libre (voir la docstring du module : cette même phrase peut apparaître ailleurs dans le
    transcript sans être le vrai reminder)."""
    model_id = None
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "attachment":
            continue
        candidate = _safe_get(entry, "attachment.identity.modelId")
        if candidate:
            model_id = candidate
    if model_id is None:
        return None
    return CEILING_1M if "[1m]" in model_id else CEILING_DEFAULT


def _last_assistant_usage(text):
    """Le champ `usage` du dernier message assistant du texte (parcours depuis la fin), ou
    `None` si aucune ligne assistant n'en porte un."""
    for raw_line in reversed(text.splitlines()):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        usage = _safe_get(entry, "message.usage")
        if usage:
            return usage
    return None


def context_window_pct(session_id, cwd):
    """`%` de context window consommé, ou `None` si le transcript, le plafond ou l'`usage` ne
    sont pas résolvables."""
    path = _transcript_path(session_id, cwd)
    if not path or not os.path.isfile(path):
        return None
    head = _head_text(path)
    ceiling = _model_ceiling_from_transcript(head) if head is not None else None
    tail = _tail_text(path)
    usage = _last_assistant_usage(tail) if tail is not None else None
    if ceiling is None or usage is None:
        return None
    consumed = (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
    )
    return round(consumed / ceiling * 100)


def _rate_limit_state_path(session_id):
    """Doit rester identique, octet pour octet, à `statusline.py::_rate_limit_state_path` — les
    deux calculs sont dupliqués volontairement (deux plugins découplés), pas partagés."""
    return os.path.join(
        tempfile.gettempdir(),
        f"pc-statusline-ratelimits-{os.getuid()}-{session_id}.json",
    )


def _read_rate_limit_state(session_id):
    """`os.getuid()` n'existe pas sous Windows — `pc-statusline` ne peut de toute façon pas y
    écrire ce fichier, donc l'absence vaut simplement « rien à lire », jamais un crash."""
    if not session_id or not hasattr(os, "getuid"):
        return None
    try:
        with open(_rate_limit_state_path(session_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def evaluate_rate_limit(session_id, now=None):
    """Pour chaque fenêtre (`five_hour`, `seven_day`), `triggered` quand le rythme de
    consommation dépasse une ligne linéaire jusqu'au reset ET que ce reset est à plus d'1h —
    `None` si le fichier d'état `pc-statusline` est absent ou ne porte aucune fenêtre exploitable."""
    state = _read_rate_limit_state(session_id)
    if not state:
        return None
    now = time.time() if now is None else now
    result = {}
    for key, window_seconds in WINDOW_SECONDS.items():
        entry = state.get(key)
        if not entry:
            continue
        used = entry.get("used_percentage")
        resets_at = entry.get("resets_at")
        if used is None or resets_at is None:
            continue
        remaining = resets_at - now
        elapsed_fraction = max(
            0.0, min(1.0, (window_seconds - remaining) / window_seconds)
        )
        used_fraction = used / 100
        result[key] = {
            "used_percentage": used,
            "resets_at": resets_at,
            "elapsed_fraction": elapsed_fraction,
            "triggered": used_fraction > elapsed_fraction and remaining > 3600,
        }
    return result or None


def main():
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    cwd = os.getcwd()
    print(
        json.dumps(
            {
                "context_window_pct": context_window_pct(session_id, cwd),
                "rate_limit": evaluate_rate_limit(session_id),
            }
        )
    )


if __name__ == "__main__":
    sys.exit(main())
