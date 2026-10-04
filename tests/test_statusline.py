"""Statusline — multi-line dashboard tests.

Covers a 3-line dashboard (+ a conditional rate-limit warning line): model/effort/mode, session
id/duration/subagent count, context bar + cache hit rate. `context_window.used_percentage` comes
straight from the harness payload.

Oracle strategy (tests.md § generator oracle): the small pure helpers (`_color_for_pct`,
`_build_bar`, `_short_model`, `_render_flex`, `_visible_len`...) each get a direct equality oracle —
sharper and far more readable than reverse-engineering one giant ANSI-laden string. `build_lines()`
itself (the `main()`-callable pure entry point, so a "never raises" test can exist without shelling
out) gets end-to-end oracles for the two cases that matter: a rich payload and an empty one.
"""

import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).parent.parent / "plugins" / "pc-statusline" / "scripts"
sys.path.insert(0, str(HOOKS))

import statusline as sl  # noqa: E402


def test_color_for_pct_thresholds():
    assert sl._color_for_pct(0) == sl.GREEN
    assert sl._color_for_pct(49) == sl.GREEN
    assert sl._color_for_pct(50) == sl.YELLOW
    assert sl._color_for_pct(74) == sl.YELLOW
    assert sl._color_for_pct(75) == sl.RED
    assert sl._color_for_pct(100) == sl.RED


def test_color_for_hit_rate_not_warmed_up_is_always_dim():
    assert sl._color_for_hit_rate(0, warmed_up=False) == "⚪️ " + sl.DIM
    assert sl._color_for_hit_rate(99, warmed_up=False) == "⚪️ " + sl.DIM


def test_color_for_hit_rate_warmed_up_thresholds():
    assert sl._color_for_hit_rate(29, warmed_up=True) == "🔴 " + sl.RED
    assert sl._color_for_hit_rate(30, warmed_up=True) == "🟡 " + sl.YELLOW
    assert sl._color_for_hit_rate(59, warmed_up=True) == "🟡 " + sl.YELLOW
    assert sl._color_for_hit_rate(60, warmed_up=True) == "🟢 " + sl.GREEN


def test_build_bar_oracle():
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
    assert sl._visible_len("🤖") == 2
    assert sl._visible_len("ab") == 2


def test_render_flex_ends_the_right_group_at_right_edge():
    assert (
        sl._render_flex(["L"], ["R"], right_edge=20, min_gap=3) == "L" + " " * 18 + "R"
    )


def test_render_flex_holds_the_right_column_when_the_left_is_empty():
    assert sl._render_flex([], ["R"], right_edge=20, min_gap=3) == " " * 19 + "R"


def test_render_flex_degrades_to_min_gap_when_groups_do_not_fit():
    assert (
        sl._render_flex(["x" * 15], ["y" * 15], right_edge=20, min_gap=4)
        == "x" * 15 + " " * 4 + "y" * 15
    )


def test_render_flex_without_right_parts_returns_left_only():
    assert sl._render_flex(["a", "b"], [], right_edge=80) == f"a{sl.SEP}b"


def test_content_widths_takes_max_per_column_not_per_row():
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


def test_fmt_duration_until_minutes_only(monkeypatch):
    import datetime as dt_mod

    class _FixedDatetime(dt_mod.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 12, 18, 0, 0)

    monkeypatch.setattr(sl, "datetime", _FixedDatetime)
    target = dt_mod.datetime(2026, 8, 12, 18, 27, 0).timestamp()
    assert sl._fmt_duration_until(target) == "27 min"


def test_fmt_duration_until_hours_and_minutes(monkeypatch):
    import datetime as dt_mod

    class _FixedDatetime(dt_mod.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 12, 18, 0, 0)

    monkeypatch.setattr(sl, "datetime", _FixedDatetime)
    target = dt_mod.datetime(2026, 8, 12, 19, 5, 0).timestamp()
    assert sl._fmt_duration_until(target) == "1h 5min"


def test_fmt_duration_until_exact_hours_has_no_trailing_minutes(monkeypatch):
    import datetime as dt_mod

    class _FixedDatetime(dt_mod.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 12, 18, 0, 0)

    monkeypatch.setattr(sl, "datetime", _FixedDatetime)
    target = dt_mod.datetime(2026, 8, 12, 20, 0, 0).timestamp()
    assert sl._fmt_duration_until(target) == "2h"


def test_fmt_duration_until_past_deadline_is_now(monkeypatch):
    import datetime as dt_mod

    class _FixedDatetime(dt_mod.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 12, 18, 0, 0)

    monkeypatch.setattr(sl, "datetime", _FixedDatetime)
    target = dt_mod.datetime(2026, 8, 12, 17, 30, 0).timestamp()
    assert sl._fmt_duration_until(target) == "now"


def test_fmt_duration_until_degrades_to_question_mark_on_garbage():
    assert sl._fmt_duration_until("not-a-timestamp") == "?"


def test_count_subagents_counts_only_matching_files(tmp_path):
    transcript = tmp_path / "main.jsonl"
    transcript.write_text("", encoding="utf-8")
    subagents_dir = tmp_path / "sess-1" / "subagents"
    subagents_dir.mkdir(parents=True)
    (subagents_dir / "agent-a.jsonl").write_text("", encoding="utf-8")
    (subagents_dir / "agent-b.jsonl").write_text("", encoding="utf-8")
    (subagents_dir / "agent-a.meta.json").write_text("{}", encoding="utf-8")
    assert sl._count_subagents(str(transcript), "sess-1") == 2


def test_count_subagents_missing_dir_is_zero_not_none(tmp_path):
    transcript = tmp_path / "main.jsonl"
    transcript.write_text("", encoding="utf-8")
    assert sl._count_subagents(str(transcript), "sess-1") == 0


def test_count_subagents_missing_inputs_is_none():
    assert sl._count_subagents("", "sess-1") is None
    assert sl._count_subagents("/x.jsonl", "") is None


import pytest as _pytest  # noqa: E402


def _stats(saved, lost, before, busts=0):
    """A minimal `/stats` payload shaped like the proxy's, keyed only where the code reads.

    `compression_vs_cache` nests under `prefix_cache` in the real payload, not at the top level.
    Getting this nesting wrong here would hide a wrong read path in the code: a lookup that never
    resolves falls back through `or 0` on both sides, which reads as "nothing to report" rather
    than as an error, so nothing fails loudly.
    """
    return {
        "prefix_cache": {
            "compression_vs_cache": {
                "tokens_saved_by_compression": saved,
                "tokens_lost_to_cache_bust": lost,
                "cache_bust_count": busts,
            }
        },
        "summary": {"compression": {"total_tokens_before": before}},
    }


@_pytest.fixture(autouse=True)
def _no_live_proxy(monkeypatch):
    """Never let the suite reach a real Headroom proxy — a developer running one would otherwise
    see different results from CI."""
    monkeypatch.setattr(sl, "_headroom_raw_stats", lambda: None)


def test_headroom_stats_net_is_savings_minus_cache_busts(monkeypatch):
    monkeypatch.setattr(
        sl, "_headroom_raw_stats", lambda: _stats(101_634, 388_415, 15_658_746, busts=4)
    )
    stats = sl._headroom_stats()
    assert stats.provider_name == "Headroom"
    assert stats.net_tokens == -286_781
    assert stats.percent == _pytest.approx(1.8317, rel=1e-3)
    assert stats.bust_count == 4


def test_headroom_stats_is_positive_when_compression_outweighs_busts(monkeypatch):
    monkeypatch.setattr(
        sl, "_headroom_raw_stats", lambda: _stats(1_400_000, 64_000, 11_000_000)
    )
    stats = sl._headroom_stats()
    assert stats.net_tokens == 1_336_000
    assert stats.percent == _pytest.approx(12.145, rel=1e-3)
    assert stats.bust_count == 0


def test_headroom_stats_none_when_proxy_is_unreachable(monkeypatch):
    monkeypatch.setattr(sl, "_headroom_raw_stats", lambda: None)
    assert sl._headroom_stats() is None


@_pytest.mark.parametrize(
    "payload",
    [
        {},
        {"summary": {}},
        {"prefix_cache": {"compression_vs_cache": {}}},
        {
            "prefix_cache": {
                "compression_vs_cache": {"tokens_saved_by_compression": "x"}
            },
            "summary": {"compression": {"total_tokens_before": 1000}},
        },
    ],
    ids=["empty", "summary-only", "compression-only", "type-poisoned"],
)
def test_headroom_stats_none_on_incomplete_or_poisoned_payload(monkeypatch, payload):
    monkeypatch.setattr(sl, "_headroom_raw_stats", lambda: payload)
    assert sl._headroom_stats() is None


def test_headroom_stats_none_when_it_exactly_breaks_even(monkeypatch):
    monkeypatch.setattr(
        sl, "_headroom_raw_stats", lambda: _stats(500, 500, 1000, busts=1)
    )
    assert sl._headroom_stats() is None


def test_fmt_compression_proxy_line_reads_as_a_gain_when_positive():
    stats = sl.ProxyStats(
        provider_name="Headroom", net_tokens=1_336_000, percent=12.145, bust_count=0
    )
    line = _strip_ansi(sl._fmt_compression_proxy_line(stats))
    assert "🤑" in line and "12%" in line and "1336k tokens saved" in line
    assert "lost" not in line and "bust" not in line


def test_fmt_compression_proxy_line_reads_as_a_loss_when_negative():
    stats = sl.ProxyStats(
        provider_name="Headroom", net_tokens=-286_781, percent=1.8317, bust_count=4
    )
    line = _strip_ansi(sl._fmt_compression_proxy_line(stats))
    assert "💸" in line and "1.8%" in line and "286k tokens lost" in line
    assert "4 cache busts" in line


def test_fmt_compression_proxy_line_singularises_a_lone_bust():
    stats = sl.ProxyStats(
        provider_name="Headroom", net_tokens=-10_000, percent=1.0, bust_count=1
    )
    line = _strip_ansi(sl._fmt_compression_proxy_line(stats))
    assert "1 cache bust" in line
    assert "1 cache busts" not in line


def test_build_lines_renders_the_headroom_row_when_the_proxy_answers(monkeypatch):
    monkeypatch.setattr(
        sl, "_headroom_raw_stats", lambda: _stats(1_400_000, 64_000, 11_000_000)
    )
    data = {
        "session_id": "s",
        "cost": {"total_cost_usd": 1.0, "total_duration_ms": 0},
        "context_window": {"used_percentage": 10, "total_input_tokens": 0},
    }
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert any(
        "Headroom 🤑" in ln and "12%" in ln and "1336k tokens saved" in ln
        for ln in stripped
    )
    assert not any("Context window" in ln and "Headroom" in ln for ln in stripped)


def test_build_lines_omits_the_headroom_row_when_the_proxy_is_down():
    stripped = [
        _strip_ansi(ln) for ln in sl.build_lines({"cost": {"total_duration_ms": 0}})
    ]
    assert not any("Headroom" in ln for ln in stripped)


def _strip_ansi(line: str) -> str:
    import re

    line = re.sub(r"\033\[[0-9;]*m", "", line)
    return re.sub(r"\033\]8;;[^\033]*\033\\", "", line)


def test_build_lines_oracle_empty_payload_still_renders_the_full_skeleton():
    """Fail-soft floor: an empty payload still returns the 4 content lines + 4 separators — the
    dashboard shape never collapses just because the fields are missing."""
    lines = sl.build_lines({})
    assert len(lines) == 8
    stripped = [_strip_ansi(ln) for ln in lines]
    assert "RATE LIMIT" not in stripped[0]
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
    assert "RATE LIMIT" not in stripped[0]
    assert "v2.1.228" in stripped[0]
    assert "🤖 Opus 4.8" in stripped[2]
    assert "💪 high" in stripped[2]
    assert "⚡️ fast" in stripped[2]
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
        {"model": "not-a-dict"},
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
    data = {"rate_limits": {"seven_day": {"used_percentage": 99, "resets_at": None}}}
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert any("Weekly: 99%" in ln for ln in stripped)
    assert not any("Hebdo" in ln for ln in stripped)


def test_build_lines_rate_limit_warning_silent_when_reset_is_imminent(monkeypatch):
    """Under `_RATE_LIMIT_MIN_RESET_S` before the reset, the window is about to renew on its own
    — a warning would be noise, so the line stays silent even well above the floor."""
    import datetime as dt_mod

    class _FixedDatetime(dt_mod.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 12, 18, 0, 0)

    monkeypatch.setattr(sl, "datetime", _FixedDatetime)
    resets_at = dt_mod.datetime(2026, 8, 12, 18, 27, 0).timestamp()
    data = {
        "rate_limits": {"five_hour": {"used_percentage": 95, "resets_at": resets_at}}
    }
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert not any("RATE LIMIT" in ln for ln in stripped)


def test_build_lines_rate_limit_warning_shows_projected_wall_time(monkeypatch):
    """Past the floor, with the reset far enough away, the line projects when the quota runs out
    at the window's average pace rather than just restating the percentage."""
    import datetime as dt_mod

    class _FixedDatetime(dt_mod.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 12, 18, 0, 0)

    monkeypatch.setattr(sl, "datetime", _FixedDatetime)
    resets_at = dt_mod.datetime(
        2026, 8, 12, 21, 0, 0
    ).timestamp()  # 2h elapsed of a 5h window
    data = {
        "rate_limits": {"five_hour": {"used_percentage": 80, "resets_at": resets_at}}
    }
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert any(
        "Session: limit in ~30 min (reset in 3h, 21:00)" in ln for ln in stripped
    )


def test_build_lines_rate_limit_weekly_wall_projection_end_to_end(monkeypatch):
    """Regression pin for the scenario that motivated the projection: 97% consumed 136h into the
    7-day window warns with a wall a few hours out, not just a raw 97%."""
    import datetime as dt_mod

    class _FixedDatetime(dt_mod.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 12, 18, 0, 0)

    monkeypatch.setattr(sl, "datetime", _FixedDatetime)
    resets_at = _FixedDatetime.now().timestamp() + 7 * 24 * 3600 - 136 * 3600
    data = {
        "rate_limits": {"seven_day": {"used_percentage": 97, "resets_at": resets_at}}
    }
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert any(
        "Weekly: limit in ~4h 12min (reset in 32h, 14/08 02:00)" in ln
        for ln in stripped
    )


def test_build_lines_rate_limit_warning_no_resets_at_falls_back_to_question_mark():
    data = {"rate_limits": {"five_hour": {"used_percentage": 95, "resets_at": None}}}
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert any("Session: 95% (reset ?)" in ln for ln in stripped)
    assert not any(" in " in ln and "RATE LIMIT" in ln for ln in stripped)


def test_build_lines_no_rate_limit_line_below_threshold():
    data = {"rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": None}}}
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert not any("RATE LIMIT" in ln for ln in stripped)


def test_build_lines_rate_limit_five_hour_floor_is_lower_than_weekly():
    """The two windows don't share a floor: 80% sits above the 5h one (75) but below the weekly
    one (90), since the 5h window burns through its quota much faster."""
    five_hour_data = {
        "rate_limits": {"five_hour": {"used_percentage": 80, "resets_at": None}}
    }
    weekly_data = {
        "rate_limits": {"seven_day": {"used_percentage": 80, "resets_at": None}}
    }
    five_hour_lines = [_strip_ansi(ln) for ln in sl.build_lines(five_hour_data)]
    weekly_lines = [_strip_ansi(ln) for ln in sl.build_lines(weekly_data)]
    assert any("Session: 80%" in ln for ln in five_hour_lines)
    assert not any("RATE LIMIT" in ln for ln in weekly_lines)


# --- _rate_limit_wall_seconds / _rate_limit_warning — the pure projection helpers, each with a
# direct equality oracle per this file's own convention (see module docstring) ------------------


def test_rate_limit_wall_seconds_oracle_pace_outruns_the_reset():
    now = 1_000_000.0
    window = 5 * 3600
    resets_at = now + 2 * 3600  # 2h left -> 3h already elapsed of the 5h window
    wall = sl._rate_limit_wall_seconds(80, resets_at, window, now=now)
    assert wall == _pytest.approx(3 * 3600 * 20 / 80)  # elapsed * (100 - used) / used


def test_rate_limit_wall_seconds_none_when_pace_lasts_until_reset():
    now = 1_000_000.0
    window = 5 * 3600
    resets_at = now + 3 * 3600  # 2h elapsed -> elapsed_fraction 0.4
    assert (
        sl._rate_limit_wall_seconds(30, resets_at, window, now=now) is None
    )  # 0.3 <= 0.4


def test_rate_limit_wall_seconds_none_when_reset_is_imminent():
    now = 1_000_000.0
    window = 5 * 3600
    resets_at = now + 1800  # 30 min left, under the 1h floor
    assert sl._rate_limit_wall_seconds(95, resets_at, window, now=now) is None


def test_rate_limit_wall_seconds_none_on_missing_usage_or_reset():
    window = 5 * 3600
    assert sl._rate_limit_wall_seconds(0, 1_000_000.0, window, now=0.0) is None
    assert sl._rate_limit_wall_seconds(50, None, window, now=0.0) is None


def test_rate_limit_warning_silent_below_each_windows_own_floor():
    assert sl._rate_limit_warning("Session", "five_hour", 74, None) is None
    assert sl._rate_limit_warning("Weekly", "seven_day", 89, None) is None


def test_rate_limit_warning_at_100pct_always_shows_the_percentage(monkeypatch):
    import datetime as dt_mod

    class _FixedDatetime(dt_mod.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 12, 18, 0, 0)

    monkeypatch.setattr(sl, "datetime", _FixedDatetime)
    resets_at = dt_mod.datetime(2026, 8, 13, 0, 0, 0).timestamp()
    warning = sl._rate_limit_warning("Weekly", "seven_day", 100, resets_at)
    assert warning == "Weekly: 100% (in 6h, reset 13/08 00:00)"


def _fixed_now_18h(monkeypatch):
    import datetime as dt_mod

    class _FixedDatetime(dt_mod.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 12, 18, 0, 0)

    monkeypatch.setattr(sl, "datetime", _FixedDatetime)
    return _FixedDatetime


def test_rate_limit_warning_next_to_100pct_shows_the_state_not_a_projection(
    monkeypatch,
):
    """Regression: 99% 128h into the weekly window used to print "limit in ~1h 18min" — a figure
    the integer percentage cannot support (the wall is anywhere between 0 and 1h 18min)."""
    fixed = _fixed_now_18h(monkeypatch)
    resets_at = fixed.now().timestamp() + 39 * 3600 + 17 * 60
    warning = sl._rate_limit_warning("Weekly", "seven_day", 99, resets_at)
    assert warning == "Weekly: 99% (in 39h 17min, reset 14/08 09:17)"
    assert "limit in" not in warning


def test_rate_limit_projection_ceiling_is_98(monkeypatch):
    fixed = _fixed_now_18h(monkeypatch)
    resets_at = fixed.now().timestamp() + 32 * 3600
    assert "limit in" in sl._rate_limit_warning("Weekly", "seven_day", 97, resets_at)
    assert "limit in" not in sl._rate_limit_warning(
        "Weekly", "seven_day", 98, resets_at
    )


def test_rate_limit_warning_next_to_100pct_stays_silent_when_reset_is_imminent(
    monkeypatch,
):
    fixed = _fixed_now_18h(monkeypatch)
    resets_at = fixed.now().timestamp() + 30 * 60
    assert sl._rate_limit_warning("Weekly", "seven_day", 99, resets_at) is None


def test_build_lines_99pct_weekly_keeps_the_yellow_background(monkeypatch):
    """Red is reserved for >= 100% (credit usage); 99% must not light it."""
    fixed = _fixed_now_18h(monkeypatch)
    resets_at = fixed.now().timestamp() + 39 * 3600
    data = {
        "rate_limits": {"seven_day": {"used_percentage": 99, "resets_at": resets_at}}
    }
    line0 = sl.build_lines(data)[0]
    assert sl.YELLOW_BG in line0
    assert sl.RED_BG not in line0


def test_rate_limit_banner_rows_pad_every_row_to_the_widest_inside_the_background():
    rows = sl._rate_limit_banner_rows(["AB", "ABCDEF"], sl.YELLOW_BG)
    assert [_strip_ansi(r) for r in rows] == [
        " ⚠ RATE LIMIT — AB     ",
        " ⚠ RATE LIMIT — ABCDEF ",
    ]
    assert len({sl._visible_len(r) for r in rows}) == 1
    # the padding sits before RESET: it is coloured, not trailing blank space
    assert all(r.startswith(sl.YELLOW_BG) and r.endswith(sl.RESET) for r in rows)


def _two_warnings_data(fixed):
    now = fixed.now().timestamp()
    return {
        "version": "2.1.289",
        "rate_limits": {
            "five_hour": {"used_percentage": 90, "resets_at": now + 2 * 3600},
            "seven_day": {"used_percentage": 99, "resets_at": now + 39 * 3600},
        },
    }


def test_build_lines_two_warnings_stack_on_two_rows_without_a_spacer(monkeypatch):
    fixed = _fixed_now_18h(monkeypatch)
    lines = sl.build_lines(_two_warnings_data(fixed))
    stripped = [_strip_ansi(ln) for ln in lines]
    assert "RATE LIMIT — Session: limit in ~20 min" in stripped[0]
    assert "RATE LIMIT — Weekly: 99%" in stripped[1]
    assert " | " not in stripped[0]
    assert (
        stripped[0].rstrip().endswith("v2.1.289")
    )  # version rides on the first row only
    assert "v2.1.289" not in stripped[1]
    assert stripped[2].strip() == sl._SPACER_ROW  # one spacer after the whole block


def test_build_lines_two_warnings_share_the_same_background_width(monkeypatch):
    fixed = _fixed_now_18h(monkeypatch)
    lines = sl.build_lines(_two_warnings_data(fixed))

    def block_width(line):
        start = line.index(sl.YELLOW_BG)
        return sl._visible_len(line[start : line.index(sl.RESET, start)])

    assert block_width(lines[0]) == block_width(lines[1])


def test_build_lines_banner_row_counts_in_the_right_column_alignment(monkeypatch):
    """Regression: the banner row was left out of the width calculation, so with a warning wider
    than the other rows the version landed at `banner + gap`, off the shared right column."""
    fixed = _fixed_now_18h(monkeypatch)
    one = {
        "version": "2.1.289",
        "rate_limits": {
            "seven_day": {
                "used_percentage": 99,
                "resets_at": fixed.now().timestamp() + 39 * 3600,
            }
        },
    }
    for data in (one, _two_warnings_data(fixed)):
        lines = sl.build_lines(data)
        banner_width = sl._visible_len(lines[0])
        model_row = next(ln for ln in lines if "🤖" in ln)
        assert sl._visible_len(model_row) == banner_width


def test_build_lines_without_warning_the_version_row_still_aligns():
    lines = sl.build_lines({"version": "2.1.289"})
    model_row = next(ln for ln in lines if "🤖" in ln)
    assert sl._visible_len(lines[0]) == sl._visible_len(model_row)


def test_rate_limit_warning_no_resets_at_falls_back_to_the_percentage():
    assert sl._rate_limit_warning("Session", "five_hour", 80, None) == (
        "Session: 80% (reset ?)"
    )


# --- rate-limit state file — the only exposure point for the server-aggregated 5h/7d quota,
# read by pc-kanban's own recommendation script ---------------------------------------------


def test_rate_limit_state_path_is_deterministic_per_session():
    assert sl._rate_limit_state_path("abc") == sl._rate_limit_state_path("abc")
    assert sl._rate_limit_state_path("abc") != sl._rate_limit_state_path("def")


def test_write_rate_limit_state_writes_both_windows(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(sl.tempfile, "gettempdir", lambda: str(tmp_path))
    data = {
        "rate_limits": {
            "five_hour": {"used_percentage": 96, "resets_at": 111},
            "seven_day": {"used_percentage": 30, "resets_at": 222},
        }
    }
    sl._write_rate_limit_state(data, "sess-1")
    with open(sl._rate_limit_state_path("sess-1"), encoding="utf-8") as f:
        state = json.load(f)
    assert state == {
        "five_hour": {"used_percentage": 96, "resets_at": 111},
        "seven_day": {"used_percentage": 30, "resets_at": 222},
    }


def test_write_rate_limit_state_no_session_id_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(sl.tempfile, "gettempdir", lambda: str(tmp_path))
    sl._write_rate_limit_state(
        {"rate_limits": {"five_hour": {"used_percentage": 99}}}, None
    )
    assert list(tmp_path.iterdir()) == []


def test_write_rate_limit_state_missing_rate_limits_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(sl.tempfile, "gettempdir", lambda: str(tmp_path))
    sl._write_rate_limit_state({}, "sess-2")
    assert list(tmp_path.iterdir()) == []


# --- non-Anthropic quota: `main()` injects the cached quota when no `rate_limits` were sent ---


def _run_main(payload, base_url, tmp_path):
    """Run the real entry point (subprocess, real stdin/stdout) with the provider host set and
    every temp file — the provider cache and the pc-kanban state — redirected into `tmp_path`."""
    import json
    import os

    env = {**os.environ, "TMPDIR": str(tmp_path)}
    env.pop("ANTHROPIC_BASE_URL", None)
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    return subprocess.run(
        [sys.executable, str(HOOKS / "statusline.py")],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        env=env,
        check=False,
    )


def _write_zai_cache(tmp_path, now, five_hour_pct, seven_day_pct):
    """A `pc-statusline-quota-zai-<uid>.json` as the hook writes it (reset times in epoch ms)."""
    import json
    import os

    def window(pct, number, reset_s):
        return {
            "type": "TOKENS_LIMIT",
            "percentage": pct,
            "number": number,
            "nextResetTime": reset_s * 1000,
        }

    payload = {
        "data": {
            "limits": [
                window(five_hour_pct, 5, now + 3600),
                window(seven_day_pct, 7, now + 300_000),
            ]
        }
    }
    path = tmp_path / f"pc-statusline-quota-zai-{os.getuid()}.json"
    path.write_text(
        json.dumps({"fetched_at": now, "payload": payload}), encoding="utf-8"
    )


def _state_file(tmp_path, session_id):
    import os

    return tmp_path / f"pc-statusline-ratelimits-{os.getuid()}-{session_id}.json"


_ZAI_BASE_URL = "https://api.z.ai/api/anthropic"


def test_main_injects_the_active_providers_quota_before_the_state_write(tmp_path):
    """End to end: no `rate_limits` in the harness payload, Z.ai as the provider -> the warning
    renders AND the pc-kanban state file carries the injected quota."""
    import json
    import time

    now = int(time.time())
    _write_zai_cache(tmp_path, now, five_hour_pct=100, seven_day_pct=30)
    result = _run_main({"session_id": "sess-e2e"}, _ZAI_BASE_URL, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Session: 100%" in _strip_ansi(result.stdout)
    state = json.loads(_state_file(tmp_path, "sess-e2e").read_text(encoding="utf-8"))
    assert state == {
        "five_hour": {"used_percentage": 100, "resets_at": now + 3600},
        "seven_day": {"used_percentage": 30, "resets_at": now + 300_000},
    }


def test_main_keeps_the_harness_rate_limits_over_the_provider_quota(tmp_path):
    import json
    import time

    now = int(time.time())
    _write_zai_cache(tmp_path, now, five_hour_pct=100, seven_day_pct=100)
    native = {"five_hour": {"used_percentage": 50, "resets_at": now + 1800}}
    result = _run_main(
        {"session_id": "sess-native", "rate_limits": native}, _ZAI_BASE_URL, tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert "RATE LIMIT" not in _strip_ansi(result.stdout)
    state = json.loads(_state_file(tmp_path, "sess-native").read_text(encoding="utf-8"))
    assert state == {"five_hour": {"used_percentage": 50, "resets_at": now + 1800}}


def test_main_never_reads_a_provider_cache_on_an_anthropic_session(tmp_path):
    """The Anthropic path stays what it was: whether the base URL is unset or its own host, a
    leftover Z.ai cache is ignored — no warning, and no state file (nothing to write)."""
    import time

    now = int(time.time())
    _write_zai_cache(tmp_path, now, five_hour_pct=100, seven_day_pct=100)
    for base_url in (None, "https://api.anthropic.com"):
        result = _run_main({"session_id": "sess-anthropic"}, base_url, tmp_path)
        assert result.returncode == 0, result.stderr
        assert "RATE LIMIT" not in _strip_ansi(result.stdout)
        assert not _state_file(tmp_path, "sess-anthropic").exists()


def test_main_is_silent_under_a_provider_whose_cache_is_missing(tmp_path):
    result = _run_main({"session_id": "sess-nocache"}, _ZAI_BASE_URL, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "RATE LIMIT" not in _strip_ansi(result.stdout)
    assert not _state_file(tmp_path, "sess-nocache").exists()


def test_main_never_crashes_on_non_object_json_under_a_provider(tmp_path):
    """The injection touches `data` before the guarded render — a non-object payload must stay a
    harmless no-op there, not a traceback."""
    for raw in ("null", "[]", "3", '"str"'):
        result = _run_main(raw, _ZAI_BASE_URL, tmp_path)
        assert result.returncode == 0, f"{raw!r}: {result.stderr}"
        assert result.stdout.strip(), f"empty stdout on {raw!r}"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_repo_branch_returns_basename_at_branch(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    assert sl._repo_branch(str(repo)) == f"my-repo{sl.SEP}🌿 main"


def test_repo_branch_walks_up_from_a_nested_subdir(tmp_path):
    repo = tmp_path / "my-repo"
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    assert sl._repo_branch(str(nested)) == f"my-repo{sl.SEP}🌿 main"


def test_repo_branch_none_outside_a_repo(tmp_path):
    assert sl._repo_branch(str(tmp_path)) is None


def test_repo_branch_none_on_falsy_input():
    assert sl._repo_branch(None) is None
    assert sl._repo_branch("") is None


def test_build_lines_shows_repo_branch_on_its_own_row_between_session_and_context(
    tmp_path,
):
    repo = tmp_path / "pinky-lib"
    repo.mkdir()
    _git("init", "-q", "-b", "feature/x", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    data = {"workspace": {"current_dir": str(repo)}}
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert f"📁 pinky-lib{_strip_ansi(sl.SEP)}🌿 feature/x" in stripped[6]
    assert "Context window" in stripped[8]


def test_build_lines_omits_the_repo_row_entirely_outside_a_repo(tmp_path):
    data = {"workspace": {"current_dir": str(tmp_path)}}
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert not any("📁" in ln for ln in stripped)
    assert "Context window" in stripped[6]


def test_build_lines_repo_row_independent_of_subagent_count_on_the_session_row(
    tmp_path, monkeypatch
):
    repo = tmp_path / "pinky-lib"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    monkeypatch.setattr(sl, "_count_subagents", lambda *_: 2)
    data = {"workspace": {"current_dir": str(repo)}, "transcript_path": "/t.jsonl"}
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert "2 subagents" in stripped[4]
    assert f"📁 pinky-lib{_strip_ansi(sl.SEP)}🌿 main" in stripped[6]


def test_forge_repo_slug_parses_ssh_and_https():
    assert sl._forge_repo_slug("git@github.com:acme/api.git") == (
        "github.com",
        "acme/api",
    )
    assert sl._forge_repo_slug("https://gitlab.com/acme/api.git") == (
        "gitlab.com",
        "acme/api",
    )
    assert sl._forge_repo_slug("https://github.com/acme/api") == (
        "github.com",
        "acme/api",
    )


def test_forge_repo_slug_none_on_unparseable_url():
    assert sl._forge_repo_slug("not a url") is None


def test_payload_repo_slug_oracle_on_the_documented_payload_shape():
    """`workspace.repo` as the statusline input schema documents it: host/owner/name, the owner
    being the full namespace path (slashes included) on a subgroup GitLab project."""
    assert sl._payload_repo_slug(
        {"host": "github.com", "owner": "anthropics", "name": "claude-code"}
    ) == ("github.com", "anthropics/claude-code")
    assert sl._payload_repo_slug(
        {"host": "gitlab.com", "owner": "group/subgroup", "name": "api"}
    ) == ("gitlab.com", "group/subgroup/api")


def test_payload_repo_slug_none_outside_a_repo_or_on_an_incomplete_block():
    for repo in (
        None,
        "text",
        3,
        {},
        {"host": "github.com"},
        {"host": "github.com", "owner": "acme", "name": ""},
    ):
        assert sl._payload_repo_slug(repo) is None


def test_repo_line_links_the_payloads_workspace_repo_block(tmp_path):
    """Regression pin: the payload's repo identity lives at `workspace.repo` per the statusline
    input schema — reading a top-level `repo` key silently disabled the shortcut and every render
    paid the `git remote get-url` subprocess. The repo below carries no `origin` remote, so the
    URL can only come from the payload block."""
    repo = tmp_path / "my-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    data = {
        "workspace": {
            "current_dir": str(repo),
            "repo": {"host": "github.com", "owner": "acme", "name": "api"},
        },
        "transcript_path": "/t.jsonl",
    }
    lines = sl.build_lines(data)
    assert any("https://github.com/acme/api" in ln for ln in lines)


def test_repo_url_github_branch_tree_when_pushed(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(repo), cwd=tmp_path)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    _git("checkout", "-q", "-b", "feature/x", cwd=repo)
    _git("push", "-q", "-u", "origin", "feature/x", cwd=repo)
    _git("remote", "set-url", "origin", "git@github.com:acme/api.git", cwd=repo)
    assert sl._repo_url(str(repo)) == "https://github.com/acme/api/tree/feature/x"


def test_repo_url_gitlab_uses_dash_tree(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(repo), cwd=tmp_path)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    _git("push", "-q", "-u", "origin", "HEAD", cwd=repo)
    _git("remote", "set-url", "origin", "git@gitlab.com:acme/api.git", cwd=repo)
    branch = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert sl._repo_url(str(repo)) == f"https://gitlab.com/acme/api/-/tree/{branch}"


def test_repo_url_falls_back_to_repo_root_without_upstream(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    _git("remote", "add", "origin", "git@github.com:acme/api.git", cwd=repo)
    assert sl._repo_url(str(repo)) == "https://github.com/acme/api"


def test_repo_url_none_without_an_origin_remote(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    assert sl._repo_url(str(repo)) is None


def test_repo_url_none_on_unrecognised_forge(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    _git("remote", "add", "origin", "git@bitbucket.org:acme/api.git", cwd=repo)
    assert sl._repo_url(str(repo)) is None


def test_build_lines_repo_row_wraps_in_a_hyperlink_when_a_forge_url_resolves(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    _git("remote", "add", "origin", "git@github.com:acme/api.git", cwd=repo)
    data = {"workspace": {"current_dir": str(repo)}}
    lines = sl.build_lines(data)
    assert (
        f"\033]8;;https://github.com/acme/api\033\\📁 my-repo{sl.SEP}🌿 main"
        in lines[6]
    )


def test_hyperlink_wraps_text_in_osc8():
    assert (
        sl._hyperlink("abc", "file:///x") == "\033]8;;file:///x\033\\abc\033]8;;\033\\"
    )


def test_visible_len_treats_a_hyperlink_as_zero_width():
    assert sl._visible_len(sl._hyperlink("abc", "file:///some/long/path")) == 3


def test_build_lines_session_id_is_shortened_to_8_chars():
    data = {"session_id": "0123456789abcdef"}
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert "Session 🆔 01234567" in stripped[4]
    assert "0123456789abcdef" not in stripped[4]


def test_build_lines_session_id_links_to_the_transcript_when_available():
    data = {"session_id": "0123456789abcdef", "transcript_path": "/tmp/x/session.jsonl"}
    lines = sl.build_lines(data)
    assert "\033]8;;file:///tmp/x/session.jsonl\033\\01234567" in lines[4]


def test_build_lines_session_id_plain_without_transcript_path():
    data = {"session_id": "0123456789abcdef"}
    lines = sl.build_lines(data)
    assert "\033]8;;" not in lines[4]
