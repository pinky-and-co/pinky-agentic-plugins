"""next_card_recommendation — session-vs-spawn signals for `/chantier`'s "Proposing the next card"
step (`kanban/references/finishing-a-card.md`). Two independent, best-effort signals: the
context-window `%` rebuilt from the session's own transcript (no `pc-statusline` dependency), and
a rate-limit pace check against `pc-statusline`'s state file (the one cross-plugin coupling
accepted, only for this axis). Each degrades to `None` rather than raising when its data source is
missing — the caller (the skill's own prose) treats `None` as "omit that part of the line", never
as an error.

Oracle strategy (tests.md § generator oracle): the pure helpers each get a direct equality oracle,
`context_window_pct`/`evaluate_rate_limit` get end-to-end oracles on the witness case from the
card's `## Done when` (`five_hour` at 96 %, reset in 1h15 on a 5h window).
"""

import json
import sys
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


def _attachment_line(model_id: str) -> str:
    """La forme réelle observée du reminder de modèle dans un vrai transcript : une entrée
    structurée `type: "attachment"`, jamais la phrase en texte libre qu'elle rend visible."""
    return json.dumps(
        {
            "type": "attachment",
            "attachment": {"type": "model", "identity": {"modelId": model_id}},
        }
    )


# --- model ceiling, from the session's own structured `attachment` entry ----------------------


def test_model_ceiling_default_without_1m_suffix():
    assert (
        ncr._model_ceiling_from_transcript(_attachment_line("claude-sonnet-5"))
        == ncr.CEILING_DEFAULT
    )


def test_model_ceiling_1m_variant():
    assert (
        ncr._model_ceiling_from_transcript(_attachment_line("claude-sonnet-5[1m]"))
        == ncr.CEILING_1M
    )


def test_model_ceiling_none_when_reminder_absent():
    assert ncr._model_ceiling_from_transcript("rien d'utile ici") is None


def test_model_ceiling_uses_the_last_occurrence():
    text = "\n".join(
        [_attachment_line("claude-sonnet-5[1m]"), _attachment_line("claude-haiku-4-5")]
    )
    assert ncr._model_ceiling_from_transcript(text) == ncr.CEILING_DEFAULT


def test_model_ceiling_ignores_the_same_sentence_quoted_in_ordinary_content():
    """Régression : le transcript peut contenir la phrase en clair ("exact model ID is …") dans
    du contenu ordinaire — une commande échoée, un fichier écrit par la session elle-même — sans
    que ce soit le vrai reminder. Seule l'entrée structurée `attachment` compte."""
    text = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "exact model ID is claude-sonnet-5.",
                            }
                        ]
                    },
                }
            ),
            _attachment_line("claude-sonnet-5[1m]"),
        ]
    )
    assert ncr._model_ceiling_from_transcript(text) == ncr.CEILING_1M


# --- last assistant usage ------------------------------------------------------------------


def test_last_assistant_usage_returns_the_most_recent_one():
    text = "\n".join(
        json.dumps(e)
        for e in [
            {"type": "assistant", "message": {"usage": {"input_tokens": 1}}},
            {"type": "user", "message": {}},
            {"type": "assistant", "message": {"usage": {"input_tokens": 42}}},
        ]
    )
    assert ncr._last_assistant_usage(text) == {"input_tokens": 42}


def test_last_assistant_usage_none_without_any_assistant_entry():
    assert ncr._last_assistant_usage("pas du json\n{}") is None


def test_last_assistant_usage_skips_assistant_entries_without_usage():
    text = "\n".join(
        json.dumps(e)
        for e in [
            {"type": "assistant", "message": {"usage": {"input_tokens": 7}}},
            {"type": "assistant", "message": {}},
        ]
    )
    assert ncr._last_assistant_usage(text) == {"input_tokens": 7}


# --- context_window_pct() end-to-end -------------------------------------------------------


def test_context_window_pct_oracle(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ncr, "_transcript_path", lambda session_id, cwd: str(tmp_path / "t.jsonl")
    )
    text = "\n".join(
        [
            _attachment_line("claude-sonnet-5[1m]"),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": 100_000,
                            "cache_read_input_tokens": 400_000,
                            "cache_creation_input_tokens": 0,
                        }
                    },
                }
            ),
        ]
    )
    (tmp_path / "t.jsonl").write_text(text, encoding="utf-8")
    assert ncr.context_window_pct("sess", str(tmp_path)) == 50


def test_context_window_pct_resolves_ceiling_from_the_head_on_a_large_session(
    tmp_path, monkeypatch
):
    """Repro observé en conditions réelles sur une session longue : l'attachment d'identité de
    modèle n'est journalisé qu'une seule fois, en tout début de fichier — bien au-delà de
    `_TAIL_BYTES` une fois la session grossie. Le plafond doit venir d'une lecture en tête de
    fichier, jamais d'un tail (qui, lui, sert uniquement à retrouver le dernier `usage`)."""
    monkeypatch.setattr(
        ncr, "_transcript_path", lambda session_id, cwd: str(tmp_path / "t.jsonl")
    )
    filler = json.dumps({"type": "user", "message": {"content": "x" * 1000}})
    lines = [_attachment_line("claude-sonnet-5[1m]")]
    lines += [filler] * 100  # ~100 Ko de remplissage, au-delà de _TAIL_BYTES par défaut
    lines.append(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 100_000,
                        "cache_read_input_tokens": 400_000,
                        "cache_creation_input_tokens": 0,
                    }
                },
            }
        )
    )
    (tmp_path / "t.jsonl").write_text("\n".join(lines), encoding="utf-8")
    assert ncr.context_window_pct("sess", str(tmp_path)) == 50


def test_context_window_pct_none_when_transcript_missing(tmp_path):
    assert ncr.context_window_pct("sess", str(tmp_path)) is None


def test_context_window_pct_none_without_session_id_or_cwd():
    assert ncr.context_window_pct(None, "/tmp") is None
    assert ncr.context_window_pct("sess", None) is None


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


def test_evaluate_rate_limit_triggers_on_the_witness_case(tmp_path, monkeypatch):
    """Le cas témoin de la carte : five_hour à 96 %, reset dans 1h15 sur une fenêtre de 5h ->
    elapsed_fraction = 75 % < 96 % et 1h15 > 1h -> déclenché."""
    monkeypatch.setattr(ncr.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ncr.os, "getuid", lambda: 501, raising=False)
    now = 1_000_000.0
    resets_at = now + 75 * 60
    Path(ncr._rate_limit_state_path("sess-2")).write_text(
        json.dumps({"five_hour": {"used_percentage": 96, "resets_at": resets_at}}),
        encoding="utf-8",
    )
    result = ncr.evaluate_rate_limit("sess-2", now=now)
    assert result["five_hour"]["triggered"] is True
    assert result["five_hour"]["elapsed_fraction"] == 0.75


def test_evaluate_rate_limit_not_triggered_when_reset_is_under_an_hour(
    tmp_path, monkeypatch
):
    """Marge < 1h avant reset -> on s'en fout, même à 99 %."""
    monkeypatch.setattr(ncr.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ncr.os, "getuid", lambda: 501, raising=False)
    now = 1_000_000.0
    resets_at = now + 30 * 60
    Path(ncr._rate_limit_state_path("sess-3")).write_text(
        json.dumps({"five_hour": {"used_percentage": 99, "resets_at": resets_at}}),
        encoding="utf-8",
    )
    result = ncr.evaluate_rate_limit("sess-3", now=now)
    assert result["five_hour"]["triggered"] is False


def test_evaluate_rate_limit_not_triggered_when_pace_is_under_elapsed(
    tmp_path, monkeypatch
):
    """Rythme sous la ligne linéaire (used_fraction < elapsed_fraction) -> pas déclenché,
    même avec plus d'1h avant le reset."""
    monkeypatch.setattr(ncr.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ncr.os, "getuid", lambda: 501, raising=False)
    now = 1_000_000.0
    resets_at = now + 75 * 60
    Path(ncr._rate_limit_state_path("sess-4")).write_text(
        json.dumps({"five_hour": {"used_percentage": 50, "resets_at": resets_at}}),
        encoding="utf-8",
    )
    result = ncr.evaluate_rate_limit("sess-4", now=now)
    assert result["five_hour"]["triggered"] is False


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
