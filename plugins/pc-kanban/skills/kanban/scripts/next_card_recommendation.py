#!/usr/bin/env python3
"""Session-vs-spawn signals for `/chantier`'s "Proposing the next card" step
(`kanban/references/finishing-a-card.md`).

Two independent, best-effort signals, each degrading to `None` rather than raising when its
data source is missing — the caller treats `None` as "omit that part of the line", never as an
error. Both come from the same per-session state file that `pc-statusline` writes on every render
(`statusline.py::_write_rate_limit_state`) — the status line command is the only place the harness
hands over `context_window.used_percentage` and `rate_limits`; the one cross-plugin coupling
accepted here.

- **`context_window_pct`** — the state file's `context_used_percentage`, whatever the model id.
  Absent when the file is missing, carries no value, or was last written more than 300 s ago
  (`None`; the transcript is never read to guess it).
- **`evaluate_rate_limit`** — `rate_limits.five_hour`/`.seven_day` are a quota aggregated
  server-side across the whole account. `five_hour` is `triggered` when `used_percentage >= 95`
  *and* the reset is more than 1h away; `seven_day` is never `triggered` (its reset is days away,
  waiting is not practicable) but is still reported.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 24 * 3600}

CONTEXT_MAX_AGE_SECONDS = 300
FIVE_HOUR_PAUSE_PCT = 95
PAUSE_MIN_REMAINING_SECONDS = 3600


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


def context_window_pct(session_id, now=None):
    """`%` de context window consommé tel que la status line l'a écrit, ou `None` si le fichier
    d'état est absent, sans valeur numérique, ou plus vieux que `CONTEXT_MAX_AGE_SECONDS`.
    Le mtime est pris avant la lecture : un rendu qui remplace le fichier entre les deux ne
    peut que vieillir la valeur, jamais rajeunir un contenu périmé."""
    if not session_id or not hasattr(os, "getuid"):
        return None
    try:
        mtime = os.path.getmtime(_rate_limit_state_path(session_id))
    except OSError:
        return None
    state = _read_rate_limit_state(session_id)
    if not isinstance(state, dict):
        return None
    pct = state.get("context_used_percentage")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    now = time.time() if now is None else now
    if now - mtime > CONTEXT_MAX_AGE_SECONDS:
        return None
    return pct


def evaluate_rate_limit(session_id, now=None):
    """Pour chaque fenêtre (`five_hour`, `seven_day`) : `triggered` uniquement pour `five_hour`,
    quand l'usage atteint `FIVE_HOUR_PAUSE_PCT` ET que le reset est à plus d'1h —
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
        result[key] = {
            "used_percentage": used,
            "resets_at": resets_at,
            "elapsed_fraction": elapsed_fraction,
            "triggered": key == "five_hour"
            and used >= FIVE_HOUR_PAUSE_PCT
            and remaining > PAUSE_MIN_REMAINING_SECONDS,
        }
    return result or None


def main():
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    print(
        json.dumps(
            {
                "context_window_pct": context_window_pct(session_id),
                "rate_limit": evaluate_rate_limit(session_id),
            }
        )
    )


if __name__ == "__main__":
    sys.exit(main())
