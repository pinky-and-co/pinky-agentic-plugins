"""next_card_recommendation — session-vs-spawn signals for `/chantier`'s "Proposing the next card"
step (`kanban/references/finishing-a-card.md`). Two independent, best-effort signals: the
context-window `%` and a rate-limit check, both read from `pc-statusline`'s per-session state file
(the one cross-plugin coupling accepted). Each degrades to `None` rather than raising when its data source is
missing — the caller (the skill's own prose) treats `None` as "omit that part of the line", never
as an error.

Oracle strategy (tests.md § generator oracle): the pure helpers each get a direct equality oracle,
`context_window_pct`/`evaluate_rate_limit` get end-to-end oracles against a state file in `tmp_path`.
"""

import json
import os
import sys
import time
from pathlib import Path

SCRIPTS = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-kanban"
    / "skills"
    / "kanban"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

import next_card_recommendation as ncr  # noqa: E402


def _isolate(tmp_path, monkeypatch):
    """Point the state file's directory at `tmp_path` so no test writes the shared temp dir."""
    monkeypatch.setattr(ncr.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ncr.os, "getuid", lambda: 501, raising=False)


def _write_state(session_id, state, mtime=None):
    path = Path(ncr._rate_limit_state_path(session_id))
    path.write_text(json.dumps(state), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


# --- context_window_pct(): the status line's own value, from the state file ------------------


def test_context_window_pct_is_the_state_files_value_whatever_the_transcript(
    tmp_path, monkeypatch
):
    """Repro: `/context` at 23 % while a 224 000-token transcript without `[1m]` read as 102 %."""
    _isolate(tmp_path, monkeypatch)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"usage": {"input_tokens": 224_000}},
            }
        ),
        encoding="utf-8",
    )
    _write_state("sess", {"context_used_percentage": 23}, mtime=1_000_000.0)
    assert ncr.context_window_pct("sess", now=1_000_010.0) == 23


def test_context_window_pct_none_without_state_even_with_a_transcript(
    tmp_path, monkeypatch
):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "t.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 1}}}),
        encoding="utf-8",
    )
    assert ncr.context_window_pct("sess", now=1_000_000.0) is None


def test_context_window_pct_none_when_state_has_no_context_value(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _write_state(
        "sess",
        {"five_hour": {"used_percentage": 10, "resets_at": 1}},
        mtime=1_000_000.0,
    )
    assert ncr.context_window_pct("sess", now=1_000_000.0) is None


def test_context_window_pct_freshness_boundary(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _write_state("sess", {"context_used_percentage": 23}, mtime=1_000_000.0)
    assert ncr.context_window_pct("sess", now=1_000_299.0) == 23
    assert ncr.context_window_pct("sess", now=1_000_300.0) == 23
    assert ncr.context_window_pct("sess", now=1_000_301.0) is None


def test_context_window_pct_ages_from_the_mtime_taken_before_the_read(
    tmp_path, monkeypatch
):
    """A writer replacing the file during the read must not make old content look fresh."""
    _isolate(tmp_path, monkeypatch)
    now = 1_000_000.0
    _write_state("sess", {"context_used_percentage": 23}, mtime=now - 400)
    real_read = ncr._read_rate_limit_state

    def read_then_bump(session_id):
        state = real_read(session_id)
        os.utime(ncr._rate_limit_state_path(session_id), (now, now))
        return state

    monkeypatch.setattr(ncr, "_read_rate_limit_state", read_then_bump)
    assert ncr.context_window_pct("sess", now=now) is None


def test_context_window_pct_none_when_state_is_not_valid_json(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    Path(ncr._rate_limit_state_path("sess")).write_text("{not json", encoding="utf-8")
    assert ncr.context_window_pct("sess", now=time.time()) is None


def test_context_window_pct_none_when_value_is_not_a_number(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _write_state("sess", {"context_used_percentage": "23"}, mtime=1_000_000.0)
    assert ncr.context_window_pct("sess", now=1_000_000.0) is None


def test_context_window_pct_none_without_session_id(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert ncr.context_window_pct(None) is None


# --- rate-limit state file + pace formula --------------------------------------------------


def test_rate_limit_state_path_matches_statuslines_convention(monkeypatch):
    monkeypatch.setattr(ncr.os, "getuid", lambda: 501, raising=False)
    assert ncr._rate_limit_state_path("sess-1").endswith(
        "pc-statusline-ratelimits-501-sess-1.json"
    )


def test_evaluate_rate_limit_none_without_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ncr.tempfile, "gettempdir", lambda: str(tmp_path))
    assert ncr.evaluate_rate_limit("session-absente") is None


def test_evaluate_rate_limit_none_without_crashing_when_getuid_is_unavailable(
    tmp_path, monkeypatch
):
    """Windows n'a pas `os.getuid` — `pc-statusline` ne peut de toute façon pas y écrire le
    fichier d'état, donc l'absence de la fonction elle-même doit rester une simple absence de
    signal, jamais une exception qui remonte jusqu'à `main()`."""
    monkeypatch.setattr(ncr.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delattr(ncr.os, "getuid", raising=False)
    assert ncr.evaluate_rate_limit("sess-windows") is None


def _five_hour(tmp_path, monkeypatch, session, used, remaining_seconds):
    _isolate(tmp_path, monkeypatch)
    now = 1_000_000.0
    _write_state(
        session,
        {"five_hour": {"used_percentage": used, "resets_at": now + remaining_seconds}},
    )
    return ncr.evaluate_rate_limit(session, now=now)["five_hour"]


def test_evaluate_rate_limit_triggers_at_95_with_reset_in_two_hours(
    tmp_path, monkeypatch
):
    result = _five_hour(tmp_path, monkeypatch, "sess-2", 95, 2 * 3600)
    assert result == {
        "used_percentage": 95,
        "resets_at": 1_000_000.0 + 2 * 3600,
        "elapsed_fraction": 0.6,
        "triggered": True,
    }


def test_evaluate_rate_limit_not_triggered_when_reset_is_under_an_hour(
    tmp_path, monkeypatch
):
    assert (
        _five_hour(tmp_path, monkeypatch, "sess-3", 95, 30 * 60)["triggered"] is False
    )


def test_evaluate_rate_limit_not_triggered_under_95_even_ahead_of_pace(
    tmp_path, monkeypatch
):
    """94 % at 10 % of the window elapsed: far ahead of a uniform pace, still no pause."""
    result = _five_hour(tmp_path, monkeypatch, "sess-4", 94, int(4.5 * 3600))
    assert result["elapsed_fraction"] == 0.1
    assert result["triggered"] is False


def test_evaluate_rate_limit_seven_day_never_triggers(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    now = 1_000_000.0
    _write_state(
        "sess-7",
        {"seven_day": {"used_percentage": 99, "resets_at": now + 3 * 24 * 3600}},
    )
    result = ncr.evaluate_rate_limit("sess-7", now=now)
    assert result["seven_day"]["used_percentage"] == 99
    assert result["seven_day"]["triggered"] is False


def test_evaluate_rate_limit_skips_a_window_missing_used_percentage(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ncr.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ncr.os, "getuid", lambda: 501, raising=False)
    Path(ncr._rate_limit_state_path("sess-5")).write_text(
        json.dumps({"five_hour": {"resets_at": 123}}), encoding="utf-8"
    )
    assert ncr.evaluate_rate_limit("sess-5", now=100.0) is None


# --- main() CLI smoke test -----------------------------------------------------------------


def test_main_prints_valid_json_and_never_crashes(tmp_path):
    import os
    import subprocess

    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "next_card_recommendation.py")],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={**os.environ, "CLAUDE_CODE_SESSION_ID": "session-inexistante"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["context_window_pct"] is None
    assert payload["rate_limit"] is None
