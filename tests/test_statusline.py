"""Statusline — multi-line dashboard rewrite (2026-08-12).

Replaces the single-segment `⬡ model · ctx · $cost · cwd` bar with a 3-line dashboard (+ a
conditional rate-limit warning line): model/effort/mode, session id/duration/subagent count,
context bar + cache hit rate. `context_window.used_percentage` now comes straight from the harness
payload — the old manual transcript-tail parsing (`_tail_usage`/`_ctx_tokens`) and its
`PINKY_CONTEXT_WINDOW`/`PINKY_WARN_PCT` overrides are gone with it.

Oracle strategy (tests.md § generator oracle): the small pure helpers (`_color_for_pct`,
`_build_bar`, `_short_model`, `_render_flex`, `_visible_len`...) each get a direct equality oracle —
sharper and far more readable than reverse-engineering one giant ANSI-laden string. `build_lines()`
itself (the `main()`-callable pure entry point restored by this fix, so a "never raises" test can
exist without shelling out) gets end-to-end oracles for the two cases that matter: a rich payload and
an empty one.
"""

import sys
from pathlib import Path

HOOKS = Path(__file__).parent.parent / "plugins" / "pc-statusline" / "scripts"
sys.path.insert(0, str(HOOKS))

import statusline as sl  # noqa: E402


# --- small pure helpers -------------------------------------------------------------------


def test_color_for_pct_thresholds():
    assert sl._color_for_pct(0) == sl.GREEN
    assert sl._color_for_pct(49) == sl.GREEN
    assert sl._color_for_pct(50) == sl.YELLOW
    assert sl._color_for_pct(74) == sl.YELLOW
    assert sl._color_for_pct(75) == sl.RED
    assert sl._color_for_pct(100) == sl.RED


def test_color_for_hit_rate_not_warmed_up_is_always_dim():
    # Cache not primed yet (no cache_creation/cache_read) — a low rate is expected, not a signal.
    assert sl._color_for_hit_rate(0, warmed_up=False) == "⚪️ " + sl.DIM
    assert sl._color_for_hit_rate(99, warmed_up=False) == "⚪️ " + sl.DIM


def test_color_for_hit_rate_warmed_up_thresholds():
    # Inverted vs. context %: here a LOW rate is the problem (more fresh tokens = higher cost).
    assert sl._color_for_hit_rate(29, warmed_up=True) == "🔴 " + sl.RED
    assert sl._color_for_hit_rate(30, warmed_up=True) == "🟡 " + sl.YELLOW
    assert sl._color_for_hit_rate(59, warmed_up=True) == "🟡 " + sl.YELLOW
    assert sl._color_for_hit_rate(60, warmed_up=True) == "🟢 " + sl.GREEN


def test_build_bar_oracle():
    # 40% of a 10-char bar = 4 filled, 6 empty, wrapped in the pct's own color (green below 70).
    assert sl._build_bar(40, width=10) == (
        f"{sl.GREEN}{'█' * 4}{sl.RESET}{sl.DIM}{'░' * 6}{sl.RESET}"
    )


def test_build_bar_clamps_out_of_range_pct():
    assert sl._build_bar(150, width=10) == sl._build_bar(100, width=10)
    assert sl._build_bar(-20, width=10) == sl._build_bar(0, width=10)


def test_short_model_oracle():
    assert sl._short_model("claude-opus-4-8") == "Opus 4.8"
    assert sl._short_model("claude-sonnet-5") == "Sonnet 5"
    assert sl._short_model("claude-haiku-4-5-20251001") == "Haiku 4.5"
    assert sl._short_model(None) is None


def test_visible_len_ignores_ansi_and_counts_emoji_as_wide():
    assert sl._visible_len(f"{sl.RED}ab{sl.RESET}") == 2
    # An emoji in the "main emoji" wide range counts as 2 columns, matching real terminal rendering.
    assert sl._visible_len("🤖") == 2
    assert sl._visible_len("ab") == 2


def test_render_flex_ends_the_right_group_at_right_edge():
    # left "L" (1 col) + right "R" (1 col), right_edge 20 -> 18 spaces, right group ends at col 20.
    assert (
        sl._render_flex(["L"], ["R"], right_edge=20, min_gap=3) == "L" + " " * 18 + "R"
    )


def test_render_flex_holds_the_right_column_when_the_left_is_empty():
    # No left content -> the right group still ends at right_edge; a shorter left just means more
    # padding. This is what removes the need for a spacer placeholder on line 0.
    assert sl._render_flex([], ["R"], right_edge=20, min_gap=3) == " " * 19 + "R"


def test_render_flex_degrades_to_min_gap_when_groups_do_not_fit():
    # left+right already wider than right_edge -> the gap floors at min_gap, the row overflows
    # rather than the two groups overlapping.
    assert (
        sl._render_flex(["x" * 15], ["y" * 15], right_edge=20, min_gap=4)
        == "x" * 15 + " " * 4 + "y" * 15
    )


def test_render_flex_without_right_parts_returns_left_only():
    assert sl._render_flex(["a", "b"], [], right_edge=80) == f"a{sl.SEP}b"


def test_content_widths_takes_max_per_column_not_per_row():
    # Row 1: left 20 / right 43 ; row 2: left 50 / right 13. max_left and max_right are measured
    # separately so every row's right group can sit at one consistent column.
    specs = [(["x" * 20], ["y" * 43]), (["x" * 50], ["y" * 13])]
    assert sl._content_widths(specs) == (50, 43)


def test_content_widths_ignores_rows_that_have_no_right_group():
    specs = [(["aaa"], []), (["a"], ["bb"])]
    assert sl._content_widths(specs) == (3, 2)


def test_fmt_epoch_same_day_shows_time_only(monkeypatch):
    import datetime as dt_mod

    class _FixedDatetime(dt_mod.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 12, 18, 0, 0)

    monkeypatch.setattr(sl, "datetime", _FixedDatetime)
    same_day = dt_mod.datetime(2026, 8, 12, 9, 30).timestamp()
    assert sl._fmt_epoch(same_day) == "09:30"


def test_fmt_epoch_different_day_shows_date_and_time(monkeypatch):
    import datetime as dt_mod

    class _FixedDatetime(dt_mod.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 12, 18, 0, 0)

    monkeypatch.setattr(sl, "datetime", _FixedDatetime)
    next_week = dt_mod.datetime(2026, 8, 19, 9, 30).timestamp()
    assert sl._fmt_epoch(next_week) == "19/08 09:30"


def test_fmt_epoch_degrades_to_question_mark_on_garbage():
    assert sl._fmt_epoch("not-a-timestamp") == "?"


def test_count_subagents_counts_only_matching_files(tmp_path):
    transcript = tmp_path / "main.jsonl"
    transcript.write_text("", encoding="utf-8")
    subagents_dir = tmp_path / "sess-1" / "subagents"
    subagents_dir.mkdir(parents=True)
    (subagents_dir / "agent-a.jsonl").write_text("", encoding="utf-8")
    (subagents_dir / "agent-b.jsonl").write_text("", encoding="utf-8")
    (subagents_dir / "agent-a.meta.json").write_text(
        "{}", encoding="utf-8"
    )  # not counted
    assert sl._count_subagents(str(transcript), "sess-1") == 2


def test_count_subagents_missing_dir_is_zero_not_none(tmp_path):
    transcript = tmp_path / "main.jsonl"
    transcript.write_text("", encoding="utf-8")
    assert sl._count_subagents(str(transcript), "sess-1") == 0


def test_count_subagents_missing_inputs_is_none():
    assert sl._count_subagents("", "sess-1") is None
    assert sl._count_subagents("/x.jsonl", "") is None


# --- _headroom_savings ---------------------------------------------------------------------

import json as _json  # noqa: E402
from datetime import datetime as _dt  # noqa: E402
from datetime import timezone as _tz  # noqa: E402


def _write_ledger(path, events):
    # real ledger events always carry `client`; default it so fixtures stay terse
    rows = [{"client": "claude-code", **e} for e in events]
    path.write_text("\n".join(_json.dumps(e) for e in rows) + "\n", encoding="utf-8")


def test_headroom_savings_aggregates_only_events_after_session_start(tmp_path):
    ledger = tmp_path / "savings_events.jsonl"
    _write_ledger(
        ledger,
        [
            {
                "ts": "2026-09-03T09:00:00+00:00",
                "before": 9999,
                "saved": 9999,
            },  # before
            {"ts": "2026-09-03T12:30:00+00:00", "before": 20000, "saved": 6000},
            {"ts": "2026-09-03T12:45:00+00:00", "before": 10000, "saved": 2000},
        ],
    )
    session_start = _dt(2026, 9, 3, 12, 0, 0, tzinfo=_tz.utc)
    # (6000 + 2000) / (20000 + 10000) = 26.66…%, 8000 tokens
    assert sl._headroom_savings(session_start, ledger_path=str(ledger)) == (
        8000 / 30000 * 100,
        8000,
    )


def test_headroom_savings_none_when_ledger_absent(tmp_path):
    session_start = _dt(2026, 9, 3, 12, 0, 0, tzinfo=_tz.utc)
    assert (
        sl._headroom_savings(session_start, ledger_path=str(tmp_path / "nope.jsonl"))
        is None
    )


def test_headroom_savings_none_when_no_event_in_window(tmp_path):
    ledger = tmp_path / "savings_events.jsonl"
    _write_ledger(
        ledger, [{"ts": "2026-09-03T09:00:00+00:00", "before": 5000, "saved": 500}]
    )
    session_start = _dt(2026, 9, 3, 12, 0, 0, tzinfo=_tz.utc)
    assert sl._headroom_savings(session_start, ledger_path=str(ledger)) is None


def test_headroom_savings_skips_malformed_lines_but_keeps_good_ones(tmp_path):
    ledger = tmp_path / "savings_events.jsonl"
    ledger.write_text(
        "not json at all\n"
        '{"ts": "no-a-date", "before": 1, "saved": 1}\n'
        '{"ts": "2026-09-03T12:10:00+00:00", "before": 4000, "saved": 1000, "client": "claude-code"}\n',
        encoding="utf-8",
    )
    session_start = _dt(2026, 9, 3, 12, 0, 0, tzinfo=_tz.utc)
    assert sl._headroom_savings(session_start, ledger_path=str(ledger)) == (25.0, 1000)


def test_headroom_savings_none_when_window_has_only_zero_saves(tmp_path):
    ledger = tmp_path / "savings_events.jsonl"
    _write_ledger(
        ledger, [{"ts": "2026-09-03T12:10:00+00:00", "before": 4000, "saved": 0}]
    )
    session_start = _dt(2026, 9, 3, 12, 0, 0, tzinfo=_tz.utc)
    assert sl._headroom_savings(session_start, ledger_path=str(ledger)) is None


def test_headroom_savings_ignores_events_from_other_clients(tmp_path):
    ledger = tmp_path / "savings_events.jsonl"
    _write_ledger(
        ledger,
        [
            {
                "ts": "2026-09-03T12:30:00+00:00",
                "before": 9000,
                "saved": 9000,
                "client": "codex",
            },
            {"ts": "2026-09-03T12:31:00+00:00", "before": 4000, "saved": 1000},
        ],
    )
    session_start = _dt(2026, 9, 3, 12, 0, 0, tzinfo=_tz.utc)
    assert sl._headroom_savings(session_start, ledger_path=str(ledger)) == (25.0, 1000)


def test_headroom_savings_reads_only_the_tail_of_a_large_ledger(tmp_path):
    # 400 ancient filler lines then one in-window event — with a small tail window the seek lands
    # mid-line, the partial first line is dropped, and only the trailing event is counted.
    ledger = tmp_path / "savings_events.jsonl"
    filler = _json.dumps(
        {
            "ts": "2020-01-01T00:00:00+00:00",
            "before": 5000,
            "saved": 5000,
            "client": "claude-code",
        }
    )
    tail = _json.dumps(
        {
            "ts": "2026-09-03T12:30:00+00:00",
            "before": 8000,
            "saved": 2000,
            "client": "claude-code",
        }
    )
    ledger.write_text("\n".join([filler] * 400 + [tail]) + "\n", encoding="utf-8")
    session_start = _dt(2026, 9, 3, 12, 0, 0, tzinfo=_tz.utc)
    assert sl._headroom_savings(
        session_start, ledger_path=str(ledger), tail_bytes=2000
    ) == (25.0, 2000)


def test_build_lines_renders_headroom_segment_when_session_has_savings(
    tmp_path, monkeypatch
):
    import datetime as dt_mod

    class _FixedDatetime(dt_mod.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 12, 18, 0, 0)

    monkeypatch.setattr(sl, "datetime", _FixedDatetime)
    ledger = tmp_path / "savings_events.jsonl"
    _write_ledger(
        ledger,
        [
            # a full day either side of the fixed "now" — outcome is tz-independent
            {"ts": "2026-08-11T18:00:00+00:00", "before": 100000, "saved": 100000},
            {"ts": "2026-08-13T18:00:00+00:00", "before": 40000, "saved": 12000},
        ],
    )
    monkeypatch.setattr(sl, "_HEADROOM_LEDGER", str(ledger))

    data = {
        "session_id": "s",
        "cost": {"total_cost_usd": 1.0, "total_duration_ms": 0},
        "context_window": {"used_percentage": 10, "total_input_tokens": 0},
    }
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert "🗜 Headroom 30% (-12k tok)" in stripped[6]


def test_build_lines_omits_headroom_segment_without_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "_HEADROOM_LEDGER", str(tmp_path / "absent.jsonl"))
    stripped = [
        _strip_ansi(ln) for ln in sl.build_lines({"cost": {"total_duration_ms": 0}})
    ]
    assert not any("Headroom" in ln for ln in stripped)


# --- build_lines() end-to-end oracles -------------------------------------------------------


def _strip_ansi(line: str) -> str:
    import re

    return re.sub(r"\033\[[0-9;]*m", "", line)


def test_build_lines_oracle_empty_payload_still_renders_the_full_skeleton():
    """Fail-soft floor: an empty payload still returns the 4 content lines + 4 separators — the
    dashboard shape never collapses just because the fields are missing."""
    lines = sl.build_lines({})
    assert len(lines) == 8
    stripped = [_strip_ansi(ln) for ln in lines]
    assert "RATE LIMIT" not in stripped[0]  # no rate-limit banner on row 0
    assert "v?" in stripped[0]
    assert "🤖 ?" in stripped[2]
    assert "Session 🆔 ?" in stripped[4]
    assert "Context window" in stripped[6] and "0%" in stripped[6]


def test_build_lines_oracle_rich_payload_readable_content(monkeypatch):
    """Content oracle (ANSI stripped for readability) on a fully-populated payload — order and
    presence of every segment, not just fragments scattered across the assertion."""
    import datetime as dt_mod

    class _FixedDatetime(dt_mod.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 12, 18, 0, 0)

    monkeypatch.setattr(sl, "datetime", _FixedDatetime)
    data = {
        "version": "2.1.228",
        "model": {"display_name": "Opus 4.8"},
        "effort": {"level": "high"},
        "thinking": {"enabled": True},
        "fast_mode": True,
        "output_style": {"name": "adhd"},
        "session_id": "abc123",
        "cost": {"total_cost_usd": 1.5, "total_duration_ms": 0},
        "context_window": {
            "used_percentage": 80,
            "total_input_tokens": 1000,
            "current_usage": {
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 700,
            },
        },
    }
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert "RATE LIMIT" not in stripped[0]  # no rate-limit banner on row 0
    assert "v2.1.228" in stripped[0]
    assert "🤖 Opus 4.8" in stripped[2]
    assert "💪 high" in stripped[2]
    assert "⚡ fast" in stripped[2]
    assert "🧠 ON" in stripped[2]
    assert "🎨 adhd" in stripped[2]
    assert "Session 🆔 abc123" in stripped[4]
    assert "Started at 12/08 18:00" in stripped[4]
    assert "Context window" in stripped[6] and "80%" in stripped[6]
    assert "Cache hit" in stripped[6] and "70%" in stripped[6]
    assert "1.50" in stripped[6]


def test_build_lines_tolerates_missing_and_wrong_shaped_nested_keys():
    """build_lines()'s own contract: missing keys and non-dict intermediate values degrade via
    `_safe_get` — this does NOT cover a field holding the wrong scalar TYPE (e.g. a string where a
    number is expected); that class is caught one level up, by `main()`'s try/except (see the
    subprocess test below) rather than guaranteed by every internal cast."""
    payloads = [
        {},
        {
            "model": "not-a-dict"
        },  # _safe_get bails as soon as a path segment isn't a dict
        {"context_window": "not-a-dict"},
        {"rate_limits": None},
    ]
    for payload in payloads:
        lines = sl.build_lines(payload)
        assert isinstance(lines, list) and lines


def test_main_never_crashes_on_adversarial_json_shapes(tmp_path):
    """The REAL invariant a hook must uphold — exercised at the `main()` level (subprocess, real
    stdin/stdout) since that's what the harness actually calls, and it's the one wrapped in the
    try/except that survives type-poisoned fields build_lines() itself doesn't guard against."""
    import json
    import subprocess

    adversarial_payloads = [
        "not even json {{{",
        "null",
        "[]",
        json.dumps({"context_window": {"used_percentage": "not-a-number"}}),
        json.dumps({"cost": {"total_duration_ms": "oops"}}),
        json.dumps({"rate_limits": {"five_hour": {"used_percentage": "oops"}}}),
    ]
    for raw in adversarial_payloads:
        result = subprocess.run(
            [sys.executable, str(HOOKS / "statusline.py")],
            input=raw,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        assert result.returncode == 0, (
            f"statusline.py exited {result.returncode} on input {raw!r}\n{result.stderr}"
        )
        assert result.stdout.strip(), (
            f"empty stdout on input {raw!r} — a blank bar is a failure too"
        )


def test_build_lines_rate_limit_warning_line_appears_above_90pct():
    data = {"rate_limits": {"five_hour": {"used_percentage": 95, "resets_at": None}}}
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert any("RATE LIMIT" in ln and "Session: 95%" in ln for ln in stripped)


def test_rate_limit_labels_are_english_no_profile_language_leak():
    # NFR-0023-01: a public plugin renders no author-locale string. The seven-day window is
    # "Weekly", never "Hebdo".
    data = {"rate_limits": {"seven_day": {"used_percentage": 99, "resets_at": None}}}
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert any("Weekly: 99%" in ln for ln in stripped)
    assert not any("Hebdo" in ln for ln in stripped)


def test_build_lines_no_rate_limit_line_below_threshold():
    data = {"rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": None}}}
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert not any("RATE LIMIT" in ln for ln in stripped)
