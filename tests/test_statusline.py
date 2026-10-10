"""Statusline — adaptive dashboard tests.

Covers the three logical rows (budget + cost, repo + session, model) drawn on four to five lines,
a rate-limit alert row above them while a window is about to run out, and the two layouts (long:
full labels, short: labels dropped) picked from the terminal width. `context_window.used_percentage`
comes straight from the harness payload; the width comes from `COLUMNS`.

Oracle strategy (tests.md § generator oracle): the small pure helpers (`_color_for_pct`,
`_build_bar`, `_short_model`, `_render_flex`, `_render_pair`, `_visible_len`...) each get a direct
equality oracle — sharper and far more readable than reverse-engineering one giant ANSI-laden
string. `build_lines()` itself (the `main()`-callable pure entry point, so a "never raises" test can
exist without shelling out) gets end-to-end oracles on a rich payload, an empty one, and both
layouts.
"""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).parent.parent / "plugins" / "pc-statusline" / "scripts"
sys.path.insert(0, str(HOOKS))

import _common  # noqa: E402
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


def test_row_width_is_both_groups_side_by_side_with_the_min_gap():
    assert sl._row_width(["ab"], ["cd"], sl.SEP, 4) == 2 + 4 + 2


def test_row_width_of_a_lone_group_carries_no_gap():
    assert sl._row_width([], ["cd"], sl.SEP, 4) == 2
    assert sl._row_width(["x", "y"], [], sl.SEP, 4) == len(f"x{_strip_ansi(sl.SEP)}y")


def test_render_pair_right_group_first_then_left_group():
    assert sl._render_pair(["L"], ["R"], right_edge=10) == [
        "⠀" + " " * 8 + "R",
        "L",
    ]


def test_render_pair_skips_an_empty_group():
    assert sl._render_pair([], ["R"], right_edge=10) == ["⠀" + " " * 8 + "R"]
    assert sl._render_pair(["L"], [], right_edge=10) == ["L"]


def test_render_pair_does_not_pad_a_right_group_wider_than_the_edge():
    assert sl._render_pair(["L"], ["R" * 20], right_edge=10) == ["R" * 20, "L"]


def test_truncate_end_marks_the_cut_with_an_ellipsis():
    assert sl._truncate_end("abcdefgh", 5) == "abcd…"
    assert sl._truncate_end("abc", 5) == "abc"
    assert sl._truncate_end("abcdef", 1) == "…"


def test_term_cols_reads_columns_from_the_environment(monkeypatch):
    monkeypatch.setenv("COLUMNS", "97")
    assert sl._term_cols() == 97


def test_term_cols_falls_back_to_the_default_when_no_width_is_readable(monkeypatch):
    def _no_ps(*_args, **_kwargs):
        raise OSError("no ps")

    monkeypatch.setenv("COLUMNS", "not-a-number")
    monkeypatch.setattr(sl.subprocess, "run", _no_ps)
    assert sl._term_cols() == sl._DEFAULT_COLS
    assert sl._term_cols(default=42) == 42


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


@_pytest.fixture(autouse=True)
def _no_live_process_table(monkeypatch, tmp_path):
    """Keep the advisor probe off the machine running the suite: `ps` answers nothing (so the
    process ancestry never reaches the `claude` hosting this session), the per-session caches land
    in `tmp_path` instead of the shared temp dir, `HOME` (hence `~/.claude/debug`) is
    `tmp_path/home`, and the environment of the suite's own session never reaches the advisor."""
    monkeypatch.setattr(_common, "_ps", lambda *_args: "")
    monkeypatch.setattr(sl.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_ADVISOR_TOOL", raising=False)


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


def _flush_right(text: str, right_edge: int) -> str:
    """A right-aligned line as the layout draws it: one braille blank standing in for the first
    padding cell (Claude Code trims leading whitespace), then spaces up to `right_edge`."""
    return "\u2800" + " " * (right_edge - sl._visible_len(text) - 1) + text


def _stripped_lines(data: dict) -> list[str]:
    return [_strip_ansi(ln) for ln in sl.build_lines(data)]


_RICH_PAYLOAD = {
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


def test_build_lines_oracle_empty_payload_still_renders_the_full_skeleton(monkeypatch):
    """Fail-soft floor: an empty payload still returns the four permanent lines (cost, budget,
    session, model) — the dashboard shape never collapses just because the fields are missing."""
    _fixed_now_18h(monkeypatch)
    assert _stripped_lines({}) == [
        _flush_right("💲 0.00 (API-equiv)", 116),
        "Context window ░░░░░░░░░░ 0%  ·  Cache hit —",
        _flush_right("Session 🆔 ?  ·  🕐 Started at 12/08 18:00", 116),
        _flush_right("🤖 ?  ·  💪 —  ·  🐢 normal  ·  🧠 off  ·  🎨 default", 116),
    ]


def test_build_lines_oracle_rich_payload_readable_content(monkeypatch):
    """Content oracle (ANSI stripped for readability) on a fully-populated payload — order and
    presence of every segment on the whole output, not fragments scattered across assertions."""
    _fixed_now_18h(monkeypatch)
    assert _stripped_lines(_RICH_PAYLOAD) == [
        _flush_right("💲 1.50 (API-equiv)", 116),
        "Context window ████████░░ 80%  ·  Cache hit 🟢 70%",
        _flush_right("Session 🆔 abc123  ·  🕐 Started at 12/08 18:00", 116),
        _flush_right("🤖 Opus 4.8  ·  💪 high  ·  ⚡️ fast  ·  🧠 ON  ·  🎨 adhd", 116),
    ]


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
    import os
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
            env={
                **os.environ,
                "TMPDIR": str(tmp_path),
                "HOME": str(tmp_path / "home"),
            },
        )
        assert result.returncode == 0, (
            f"statusline.py exited {result.returncode} on input {raw!r}\n{result.stderr}"
        )
        assert result.stdout.strip(), (
            f"empty stdout on input {raw!r} — a blank bar is a failure too"
        )


def _alert_lines(stripped: list[str]) -> list[str]:
    """The rate-limit alert rows of a rendered status line: the only rows that open on a window
    label (`5h` / `7d`) followed by a `━`/`─` bar."""
    return [ln for ln in stripped if ln.startswith(("5h ━", "5h ─", "7d ━", "7d ─"))]


def test_build_lines_rate_limit_alert_row_appears_above_the_floor():
    data = {"rate_limits": {"five_hour": {"used_percentage": 95, "resets_at": None}}}
    stripped = _stripped_lines(data)
    assert _alert_lines(stripped) == ["5h ━━━━━━━━━━ 95% (reset ?)"]
    assert stripped[0] == "5h ━━━━━━━━━━ 95% (reset ?)"  # the alert row comes first


def test_rate_limit_labels_are_english_no_profile_language_leak():
    data = {"rate_limits": {"seven_day": {"used_percentage": 99, "resets_at": None}}}
    stripped = _stripped_lines(data)
    assert _alert_lines(stripped) == ["7d ━━━━━━━━━━ 99% (reset ?)"]
    assert not any("Hebdo" in ln for ln in stripped)


def test_build_lines_rate_limit_alert_silent_when_reset_is_imminent(monkeypatch):
    """Under `_RATE_LIMIT_MIN_RESET_S` before the reset, the window is about to renew on its own
    — an alert would be noise, so the row stays silent even well above the floor."""
    fixed = _fixed_now_18h(monkeypatch)
    resets_at = fixed(2026, 8, 12, 18, 27, 0).timestamp()
    data = {
        "rate_limits": {"five_hour": {"used_percentage": 95, "resets_at": resets_at}}
    }
    assert _alert_lines(_stripped_lines(data)) == []


def test_build_lines_rate_limit_alert_shows_projected_wall_time(monkeypatch):
    """Past the floor, with the reset far enough away, the row projects when the quota runs out
    at the window's average pace rather than just restating the percentage."""
    fixed = _fixed_now_18h(monkeypatch)
    resets_at = fixed(
        2026, 8, 12, 21, 0, 0
    ).timestamp()  # 2h écoulées d'une fenêtre de 5h
    data = {
        "rate_limits": {"five_hour": {"used_percentage": 80, "resets_at": resets_at}}
    }
    assert _alert_lines(_stripped_lines(data)) == [
        "5h ━━━━━━━━── 80% limit in ~30 min (reset in 3h, 21:00)"
    ]


def test_build_lines_rate_limit_weekly_wall_projection_end_to_end(monkeypatch):
    """Pin for the scenario that motivated the projection: 97% consumed 136h into the 7-day
    window alerts with a wall a few hours out, not just a raw 97%."""
    fixed = _fixed_now_18h(monkeypatch)
    resets_at = fixed.now().timestamp() + 7 * 24 * 3600 - 136 * 3600
    data = {
        "rate_limits": {"seven_day": {"used_percentage": 97, "resets_at": resets_at}}
    }
    assert _alert_lines(_stripped_lines(data)) == [
        "7d ━━━━━━━━━━ 97% limit in ~4h 12min (reset in 1d 8h, 14/08 02:00)"
    ]


def test_build_lines_rate_limit_alert_no_resets_at_falls_back_to_question_mark():
    data = {"rate_limits": {"five_hour": {"used_percentage": 95, "resets_at": None}}}
    alerts = _alert_lines(_stripped_lines(data))
    assert alerts == ["5h ━━━━━━━━━━ 95% (reset ?)"]
    assert "limit in" not in alerts[0]


def test_build_lines_no_rate_limit_line_below_threshold():
    data = {"rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": None}}}
    assert _alert_lines(_stripped_lines(data)) == []


def test_build_lines_rate_limit_five_hour_floor_is_lower_than_weekly():
    """The two windows don't share a floor: 80% sits above the 5h one (75) but below the weekly
    one (90), since the 5h window burns through its quota much faster."""
    five_hour_data = {
        "rate_limits": {"five_hour": {"used_percentage": 80, "resets_at": None}}
    }
    weekly_data = {
        "rate_limits": {"seven_day": {"used_percentage": 80, "resets_at": None}}
    }
    assert _alert_lines(_stripped_lines(five_hour_data)) == [
        "5h ━━━━━━━━── 80% (reset ?)"
    ]
    assert _alert_lines(_stripped_lines(weekly_data)) == []


# --- _rate_limit_wall_seconds / _rate_limit_detail / _rate_limit_alert — the pure projection and
# alert helpers, each with a direct equality oracle per this file's own convention --------------


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


def _fixed_now_18h(monkeypatch):
    import datetime as dt_mod

    class _FixedDatetime(dt_mod.datetime):
        @classmethod
        def now(cls):
            return cls(2026, 8, 12, 18, 0, 0)

    monkeypatch.setattr(sl, "datetime", _FixedDatetime)
    return _FixedDatetime


def test_rate_limit_detail_silent_below_each_windows_own_floor():
    assert sl._rate_limit_detail("five_hour", 74, None) is None
    assert sl._rate_limit_detail("seven_day", 89, None) is None


def test_rate_limit_detail_at_100pct_always_shows_the_state(monkeypatch):
    fixed = _fixed_now_18h(monkeypatch)
    resets_at = fixed(2026, 8, 13, 0, 0, 0).timestamp()
    assert sl._rate_limit_detail("seven_day", 100, resets_at) == (
        "(in 6h, reset 13/08 00:00)"
    )


def test_rate_limit_detail_next_to_100pct_shows_the_state_not_a_projection(monkeypatch):
    """99% 128h into the weekly window used to print "limit in ~1h 18min" — a figure the integer
    percentage cannot support (the wall is anywhere between 0 and 1h 18min)."""
    fixed = _fixed_now_18h(monkeypatch)
    resets_at = fixed.now().timestamp() + 39 * 3600 + 17 * 60
    detail = sl._rate_limit_detail("seven_day", 99, resets_at)
    assert detail == "(in 1d 15h, reset 14/08 09:17)"
    assert "limit in" not in detail


def test_rate_limit_projection_ceiling_is_98(monkeypatch):
    fixed = _fixed_now_18h(monkeypatch)
    resets_at = fixed.now().timestamp() + 32 * 3600
    assert "limit in" in sl._rate_limit_detail("seven_day", 97, resets_at)
    assert "limit in" not in sl._rate_limit_detail("seven_day", 98, resets_at)


def test_rate_limit_detail_next_to_100pct_stays_silent_when_reset_is_imminent(
    monkeypatch,
):
    fixed = _fixed_now_18h(monkeypatch)
    resets_at = fixed.now().timestamp() + 30 * 60
    assert sl._rate_limit_detail("seven_day", 99, resets_at) is None


def test_rate_limit_detail_no_resets_at_falls_back_to_question_mark():
    assert sl._rate_limit_detail("five_hour", 80, None) == "(reset ?)"


def test_rate_limit_detail_short_form_keeps_the_wall_and_drops_the_reset(monkeypatch):
    fixed = _fixed_now_18h(monkeypatch)
    projected = fixed(2026, 8, 12, 21, 0, 0).timestamp()
    assert sl._rate_limit_detail("five_hour", 80, projected, short=True) == (
        "limit in ~30 min"
    )
    assert sl._rate_limit_detail("seven_day", 100, projected, short=True) == (
        "reset in 3h"
    )


def test_rate_limit_alert_oracle_is_yellow_below_100_and_red_at_100():
    """Red is reserved for an exhausted window (>= 100%); 99% must not light it."""
    yellow = sl._rate_limit_alert(
        "seven_day", {"rate_limits": {"seven_day": {"used_percentage": 99}}}
    )
    red = sl._rate_limit_alert(
        "five_hour", {"rate_limits": {"five_hour": {"used_percentage": 100}}}
    )
    assert yellow == (
        f"{sl.YELLOW}7d{sl.RESET} {sl.YELLOW}{'━' * 10}{sl.RESET}{sl.DIM}{sl.RESET} "
        f"{sl.YELLOW}99% (reset ?){sl.RESET}"
    )
    assert red == (
        f"{sl.RED}5h{sl.RESET} {sl.RED}{'━' * 10}{sl.RESET}{sl.DIM}{sl.RESET} "
        f"{sl.RED}100% (reset ?){sl.RESET}"
    )


def test_rate_limit_alert_is_none_while_the_window_is_quiet():
    assert sl._rate_limit_alert("five_hour", {}) is None
    assert (
        sl._rate_limit_alert(
            "five_hour", {"rate_limits": {"five_hour": {"used_percentage": 10}}}
        )
        is None
    )


def _two_alerts_data(fixed):
    now = fixed.now().timestamp()
    return {
        "rate_limits": {
            "five_hour": {"used_percentage": 90, "resets_at": now + 2 * 3600},
            "seven_day": {"used_percentage": 99, "resets_at": now + 39 * 3600},
        },
    }


def test_build_lines_two_alerts_share_one_row_five_hour_left_seven_day_right(
    monkeypatch,
):
    fixed = _fixed_now_18h(monkeypatch)
    stripped = _stripped_lines(_two_alerts_data(fixed))
    left = "5h ━━━━━━━━━─ 90% limit in ~20 min (reset in 2h, 20:00)"
    right = "7d ━━━━━━━━━━ 99% (in 1d 15h, reset 14/08 09:00)"
    gap = 116 - sl._visible_len(left) - sl._visible_len(right)
    assert stripped[0] == left + " " * gap + right
    assert _alert_lines(stripped) == [stripped[0]]


def test_build_lines_two_alerts_take_the_short_form_when_the_long_one_overflows(
    monkeypatch,
):
    fixed = _fixed_now_18h(monkeypatch)
    data = {**_two_alerts_data(fixed), "_term": 100}
    stripped = _stripped_lines(data)
    left = "5h ━━━━━━━━━─ 90% limit in ~20 min"
    right = "7d ━━━━━━━━━━ 99% reset in 1d 15h"
    gap = 96 - sl._visible_len(left) - sl._visible_len(right)
    assert stripped[0] == left + " " * gap + right


def test_build_lines_right_groups_end_on_the_same_column(monkeypatch):
    """Every right-aligned line ends on `COLUMNS` minus the right margin, alert row included."""
    fixed = _fixed_now_18h(monkeypatch)
    lines = sl.build_lines(_two_alerts_data(fixed))
    right_aligned = [ln for ln in lines if ln.startswith("\u2800") or "7d" in ln]
    assert len(right_aligned) == 4
    assert {sl._visible_len(ln) for ln in right_aligned} == {116}


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


def test_write_rate_limit_state_carries_context_percentage_with_rate_limits(
    tmp_path, monkeypatch
):
    import json

    monkeypatch.setattr(sl.tempfile, "gettempdir", lambda: str(tmp_path))
    sl._write_rate_limit_state(
        {
            "context_window": {"used_percentage": 23},
            "rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": 1}},
        },
        "sess-ctx",
    )
    with open(sl._rate_limit_state_path("sess-ctx"), encoding="utf-8") as f:
        assert json.load(f) == {
            "five_hour": {"used_percentage": 10, "resets_at": 1},
            "context_used_percentage": 23,
        }


def test_write_rate_limit_state_carries_context_percentage_without_rate_limits(
    tmp_path, monkeypatch
):
    import json

    monkeypatch.setattr(sl.tempfile, "gettempdir", lambda: str(tmp_path))
    sl._write_rate_limit_state({"context_window": {"used_percentage": 23}}, "sess-ctx2")
    with open(sl._rate_limit_state_path("sess-ctx2"), encoding="utf-8") as f:
        assert json.load(f) == {"context_used_percentage": 23}


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

    env = {**os.environ, "TMPDIR": str(tmp_path), "HOME": str(tmp_path / "home")}
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
    assert _alert_lines(_strip_ansi(result.stdout).splitlines())[0].startswith(
        "5h ━━━━━━━━━━ 100%"
    )
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
    assert _alert_lines(_strip_ansi(result.stdout).splitlines()) == []
    state = json.loads(_state_file(tmp_path, "sess-native").read_text(encoding="utf-8"))
    assert state == {"five_hour": {"used_percentage": 50, "resets_at": now + 1800}}


def test_main_never_reads_a_provider_cache_on_an_anthropic_session(tmp_path):
    """The Anthropic path stays what it was: whether the base URL is unset or its own host, a
    leftover Z.ai cache is ignored — no alert row, and no state file (nothing to write)."""
    import time

    now = int(time.time())
    _write_zai_cache(tmp_path, now, five_hour_pct=100, seven_day_pct=100)
    for base_url in (None, "https://api.anthropic.com"):
        result = _run_main({"session_id": "sess-anthropic"}, base_url, tmp_path)
        assert result.returncode == 0, result.stderr
        assert _alert_lines(_strip_ansi(result.stdout).splitlines()) == []
        assert not _state_file(tmp_path, "sess-anthropic").exists()


def test_main_is_silent_under_a_provider_whose_cache_is_missing(tmp_path):
    result = _run_main({"session_id": "sess-nocache"}, _ZAI_BASE_URL, tmp_path)
    assert result.returncode == 0, result.stderr
    assert _alert_lines(_strip_ansi(result.stdout).splitlines()) == []
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


def test_build_lines_shows_repo_branch_on_its_own_line_between_session_and_model(
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
    data = {"workspace": {"project_dir": str(repo)}}
    stripped = _stripped_lines(data)
    assert "Context window" in stripped[1]
    assert "Session 🆔" in stripped[2]
    assert stripped[3] == f"📁 pinky-lib{_strip_ansi(sl.SEP)}🌿 feature/x"
    assert "🤖" in stripped[4]


def test_build_lines_omits_the_repo_line_entirely_outside_a_repo(tmp_path):
    data = {"workspace": {"project_dir": str(tmp_path)}}
    stripped = _stripped_lines(data)
    assert not any("📁" in ln for ln in stripped)
    assert len(stripped) == 4
    assert "Context window" in stripped[1]


def test_build_lines_repo_line_independent_of_subagent_count_on_the_budget_line(
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
    data = {"workspace": {"project_dir": str(repo)}, "transcript_path": "/t.jsonl"}
    stripped = _stripped_lines(data)
    assert "2 subagents" in stripped[1]
    assert stripped[3] == f"📁 pinky-lib{_strip_ansi(sl.SEP)}🌿 main"


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
            "project_dir": str(repo),
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


def test_build_lines_repo_line_wraps_in_a_hyperlink_when_a_forge_url_resolves(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    _git("remote", "add", "origin", "git@github.com:acme/api.git", cwd=repo)
    data = {"workspace": {"project_dir": str(repo)}}
    lines = sl.build_lines(data)
    assert lines[3] == (
        f"\033]8;;https://github.com/acme/api\033\\📁 my-repo{sl.SEP}🌿 main\033]8;;\033\\"
    )


def test_hyperlink_wraps_text_in_osc8():
    assert (
        sl._hyperlink("abc", "file:///x") == "\033]8;;file:///x\033\\abc\033]8;;\033\\"
    )


def test_visible_len_treats_a_hyperlink_as_zero_width():
    assert sl._visible_len(sl._hyperlink("abc", "file:///some/long/path")) == 3


def test_build_lines_session_id_is_shortened_to_8_chars():
    data = {"session_id": "0123456789abcdef"}
    stripped = _stripped_lines(data)
    assert "Session 🆔 01234567" in stripped[2]
    assert "0123456789abcdef" not in stripped[2]


def test_build_lines_session_id_links_to_the_transcript_when_available():
    data = {"session_id": "0123456789abcdef", "transcript_path": "/tmp/x/session.jsonl"}
    lines = sl.build_lines(data)
    assert "\033]8;;file:///tmp/x/session.jsonl\033\\01234567" in lines[2]


def test_build_lines_session_id_plain_without_transcript_path():
    data = {"session_id": "0123456789abcdef"}
    lines = sl.build_lines(data)
    assert "\033]8;;" not in lines[2]


# --- adaptive layout — long (full labels) or short (labels dropped), chosen from `COLUMNS` ---


def _render_through_main(monkeypatch, capsys, payload, columns):
    """Run `main()` in-process the way the harness does — payload on stdin, width in `COLUMNS` —
    and return the stripped lines it printed."""
    monkeypatch.setenv("COLUMNS", str(columns))
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    sl.main()
    return [_strip_ansi(ln) for ln in capsys.readouterr().out.splitlines()]


def test_main_long_layout_carries_the_full_labels_at_160_columns(monkeypatch, capsys):
    _fixed_now_18h(monkeypatch)
    out = _render_through_main(monkeypatch, capsys, _RICH_PAYLOAD, 160)
    assert out == [
        _flush_right("💲 1.50 (API-equiv)", 156),
        "Context window ████████░░ 80%  ·  Cache hit 🟢 70%",
        _flush_right("Session 🆔 abc123  ·  🕐 Started at 12/08 18:00", 156),
        _flush_right("🤖 Opus 4.8  ·  💪 high  ·  ⚡️ fast  ·  🧠 ON  ·  🎨 adhd", 156),
    ]


def test_main_short_layout_drops_the_labels_at_60_columns(monkeypatch, capsys):
    _fixed_now_18h(monkeypatch)
    out = _render_through_main(monkeypatch, capsys, _RICH_PAYLOAD, 60)
    assert out == [
        _flush_right("💲 1.50 (API-equiv)", 56),
        "████████░░ 80% · Cache hit 🟢 70%",
        _flush_right("🆔 abc123 · 🕐 18:00", 56),
        _flush_right("🤖 Opus 4.8 · 💪 high · ⚡️ fast", 56),
    ]
    assert not any("Context window" in ln or "Started at" in ln for ln in out)


def test_main_reads_columns_afresh_on_every_refresh(monkeypatch, capsys):
    _fixed_now_18h(monkeypatch)
    wide = _render_through_main(monkeypatch, capsys, _RICH_PAYLOAD, 160)
    narrow = _render_through_main(monkeypatch, capsys, _RICH_PAYLOAD, 60)
    assert any("Context window" in ln for ln in wide)
    assert not any("Context window" in ln for ln in narrow)


def test_build_lines_picks_the_long_layout_only_when_every_row_fits():
    """The widest logical row is budget + cost: `Context window ██████████ 80%  ·  Cache hit 🟢
    70%` (50 columns) + the 4-column minimum gap + `💲 1.50 (API-equiv)` (19 columns) = 73, which
    must fit `_term` minus the 4-column right margin."""
    fits = _stripped_lines({**_RICH_PAYLOAD, "_term": 77})
    overflows = _stripped_lines({**_RICH_PAYLOAD, "_term": 76})
    assert any("Context window" in ln for ln in fits)
    assert not any("Context window" in ln for ln in overflows)


def test_build_lines_short_layout_truncates_the_branch_to_the_room_left(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "feature/" + "x" * 60, cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    data = {**_RICH_PAYLOAD, "workspace": {"project_dir": str(repo)}, "_term": 60}
    repo_line = _stripped_lines(data)[3]
    assert repo_line.startswith("📁 my-repo · 🌿 feature/")
    assert repo_line.endswith("…")
    assert "x" * 60 not in repo_line


def test_build_lines_falls_back_to_cwd_for_the_repo_when_there_is_no_project_dir(
    tmp_path,
):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    stripped = _stripped_lines({"cwd": str(repo)})
    assert stripped[3] == f"📁 my-repo{_strip_ansi(sl.SEP)}🌿 main"


# --- advisor detection — the `--advisor` value of the `claude` process, never its command line -


def test_get_advisor_model_reads_the_per_session_cache_without_running_ps(
    tmp_path, monkeypatch
):
    def _ps_must_not_run(*_args):
        raise AssertionError("ps ran despite a cached advisor")

    (tmp_path / "statusline-advisor-s1.json").write_text(
        json.dumps({"advisor": "haiku"}), encoding="utf-8"
    )
    monkeypatch.setattr(_common, "_ps", _ps_must_not_run)
    assert sl._get_advisor_model("s1") == "haiku"
    assert _common.process_flags("s1") == _common.ProcessFlags("haiku", False)


def test_get_advisor_model_does_not_cache_a_failed_ps(tmp_path, monkeypatch):
    def _failing_ps(*_args):
        raise subprocess.SubprocessError("ps failed")

    monkeypatch.setattr(_common, "_ps", _failing_ps)
    assert sl._get_advisor_model("s1") is None
    assert list(tmp_path.iterdir()) == []


@_pytest.mark.parametrize(
    "flag, expected",
    [("--advisor opus", "opus"), ("--advisor=claude-opus-4-8", "claude-opus-4-8")],
    ids=["space", "equals"],
)
def test_get_advisor_model_reads_both_flag_spellings(
    tmp_path, monkeypatch, flag, expected
):
    monkeypatch.setattr(
        _common, "_ps", lambda *_args: f"claude --session-id s1 {flag}\n"
    )
    assert sl._get_advisor_model("s1") == expected
    cache = tmp_path / "statusline-advisor-s1.json"
    assert json.loads(cache.read_text(encoding="utf-8")) == {
        "advisor": expected,
        "debug": False,
    }


_CANARY = "sk-test-" + "123"
_CANARY_ARG = "API_" + "KEY=" + _CANARY


@_pytest.mark.parametrize(
    "ps_table",
    [
        # trouvé par l'id de session dans la ligne de commande
        lambda args: f"claude --session-id sess-adv --advisor opus {_CANARY_ARG}\n",
        # id absent de la ligne (après /clear) : trouvé en remontant les ancêtres
        lambda args: (
            "" if args[0] == "-axo" else f"1 claude --advisor opus {_CANARY_ARG}\n"
        ),
    ],
    ids=["by-session-id", "by-ancestry"],
)
def test_main_persists_and_prints_only_the_advisor_value_never_the_command_line(
    tmp_path, monkeypatch, capsys, ps_table
):
    """The process command line can carry secrets (an API key in the environment string, a token
    in a flag): only the `--advisor` value may reach the cache file or the rendered line."""
    monkeypatch.setattr(_common, "_ps", lambda *args: ps_table(args))
    out = _render_through_main(
        monkeypatch, capsys, {**_RICH_PAYLOAD, "session_id": "sess-adv"}, 160
    )
    assert any("🧐 Opus" in ln for ln in out)  # la ligne a bien été lue et analysée
    assert not any(_CANARY in ln or "API_KEY" in ln for ln in out)
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written
    for path in written:
        assert _CANARY not in path.read_text(encoding="utf-8")
    cache = tmp_path / "statusline-advisor-sess-adv.json"
    assert json.loads(cache.read_text(encoding="utf-8")) == {
        "advisor": "opus",
        "debug": False,
    }


# --- advisor read from the transcript — the last `advisor_tool` entry decides, a failed read
# never erases what is shown ----------------------------------------------------------------

_NO_FLAG_PS = "claude --session-id sess-tr --effort high\n"
_OPUS_FLAG_PS = "claude --session-id sess-tr --advisor opus\n"
_ADVISOR_MARK = "🧐"


def _entry_line(available, model=None, change="add"):
    """One `advisor_tool` attachment line, as the harness writes it."""
    attachment = {"type": "advisor_tool", "available": available, "toolChange": change}
    if model:
        attachment["model"] = model
    return json.dumps({"type": "attachment", "attachment": attachment})


def _message_line(advisor_model="claude-opus-5-5", text="working on it"):
    """An assistant message: it carries `advisorModel` whatever the advisor's state is."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {"content": text},
            "advisorModel": advisor_model,
        }
    )


def _append_lines(path, *lines):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("".join(f"{line}\n" for line in lines))


def _render_advisor(monkeypatch, capsys, transcript, session_id="sess-tr"):
    """The model row's advisor segment after one `main()` render, or None when there is none."""
    payload = {
        **_RICH_PAYLOAD,
        "session_id": session_id,
        "transcript_path": str(transcript),
    }
    out = _render_through_main(monkeypatch, capsys, payload, 160)
    rows = [ln for ln in out if _ADVISOR_MARK in ln]
    return rows[0].split(_ADVISOR_MARK)[1].split("·")[0].strip() if rows else None


def test_main_shows_the_model_of_the_last_add_entry(tmp_path, monkeypatch, capsys):
    transcript = tmp_path / "session.jsonl"
    _append_lines(transcript, _entry_line(True, "claude-opus-5-5"), _message_line())
    assert _render_advisor(monkeypatch, capsys, transcript) == "Opus 5.5"


def test_main_last_entry_wins_whatever_its_tool_change(tmp_path, monkeypatch, capsys):
    transcript = tmp_path / "session.jsonl"
    _append_lines(
        transcript,
        _entry_line(True, "claude-opus-5-5"),
        _entry_line(True, "claude-sonnet-5", change="update"),
    )
    assert _render_advisor(monkeypatch, capsys, transcript) == "Sonnet 5"


def test_main_a_remove_entry_hides_the_advisor_despite_advisor_model_messages(
    tmp_path, monkeypatch, capsys
):
    """`remove` wins over the `--advisor` flag and over every message still carrying
    `advisorModel`; lines that only quote `advisor_tool` are not entries."""
    monkeypatch.setattr(_common, "_ps", lambda *_args: _OPUS_FLAG_PS)
    transcript = tmp_path / "session.jsonl"
    quoting = json.dumps(
        {
            "type": "assistant",
            "attachment": {"type": "advisor_tool", "available": True, "model": "x-y"},
        }
    )
    _append_lines(
        transcript,
        _entry_line(True, "claude-opus-5-5"),
        _entry_line(False, change="remove"),
        *[_message_line() for _ in range(5)],
        _message_line(text="the advisor_tool attachment says available: true"),
        quoting,
        "{not json but advisor_tool",
    )
    assert _render_advisor(monkeypatch, capsys, transcript) is None


def test_main_without_an_entry_falls_back_to_the_advisor_flag(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(_common, "_ps", lambda *_args: _OPUS_FLAG_PS)
    transcript = tmp_path / "session.jsonl"
    _append_lines(transcript, _message_line(), _message_line())
    assert _render_advisor(monkeypatch, capsys, transcript) == "Opus"


def test_main_takes_an_entry_written_mid_session_at_the_next_render(
    tmp_path, monkeypatch, capsys
):
    """A first render with no flag and no entry; the entry arrives; the very next render shows
    it — "no flag" was only ever the `ps` result, never a final answer."""
    monkeypatch.setattr(_common, "_ps", lambda *_args: _NO_FLAG_PS)
    transcript = tmp_path / "session.jsonl"
    _append_lines(transcript, _message_line())
    assert _render_advisor(monkeypatch, capsys, transcript) is None
    _append_lines(transcript, _entry_line(True, "claude-opus-5-5"))
    assert _render_advisor(monkeypatch, capsys, transcript) == "Opus 5.5"


def test_main_disabled_advisor_tool_env_shows_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(_common, "_ps", lambda *_args: _OPUS_FLAG_PS)
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_ADVISOR_TOOL", "1")
    transcript = tmp_path / "session.jsonl"
    _append_lines(transcript, _entry_line(True, "claude-opus-5-5"))
    assert _render_advisor(monkeypatch, capsys, transcript) is None


def test_main_keeps_the_advisor_when_the_transcript_is_deleted_or_truncated(
    tmp_path, monkeypatch, capsys
):
    transcript = tmp_path / "session.jsonl"
    _append_lines(transcript, _entry_line(True, "claude-opus-5-5"), _message_line())
    assert _render_advisor(monkeypatch, capsys, transcript) == "Opus 5.5"

    def _failing_ps(*_args):
        raise subprocess.SubprocessError("ps failed")

    monkeypatch.setattr(_common, "_ps", _failing_ps)
    transcript.unlink()
    assert _render_advisor(monkeypatch, capsys, transcript) == "Opus 5.5"
    # recreated smaller than the stored offset, with no entry: rescanned, nothing found, kept
    transcript.write_text(_message_line(text="x") + "\n", encoding="utf-8")
    assert _render_advisor(monkeypatch, capsys, transcript) == "Opus 5.5"
    # a `remove` is the only thing that takes it away
    _append_lines(transcript, _entry_line(False, change="remove"))
    assert _render_advisor(monkeypatch, capsys, transcript) is None
    transcript.unlink()
    assert _render_advisor(monkeypatch, capsys, transcript) is None


def test_main_never_advances_the_offset_past_a_half_written_line(
    tmp_path, monkeypatch, capsys
):
    transcript = tmp_path / "session.jsonl"
    complete = _entry_line(True, "claude-opus-5-5") + "\n"
    removal = _entry_line(False, change="remove")
    cut = len(removal) // 2
    transcript.write_text(complete + removal[:cut], encoding="utf-8")
    assert _render_advisor(monkeypatch, capsys, transcript) == "Opus 5.5"
    state = json.loads(
        (tmp_path / "statusline-advisor-transcript-sess-tr.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["offset"] == len(complete.encode("utf-8"))

    with open(transcript, "a", encoding="utf-8") as handle:
        handle.write(removal[cut:] + "\n")
    assert _render_advisor(monkeypatch, capsys, transcript) is None


def test_main_reads_a_finished_entry_that_has_no_newline_yet(
    tmp_path, monkeypatch, capsys
):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(_entry_line(True, "claude-opus-5-5"), encoding="utf-8")
    assert _render_advisor(monkeypatch, capsys, transcript) == "Opus 5.5"


class _CountingFile:
    """A binary file whose reads are tallied in `counter["bytes"]`."""

    def __init__(self, handle, counter):
        self._handle = handle
        self._counter = counter

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self._handle.close()

    def seek(self, offset, whence=0):
        return self._handle.seek(offset, whence)

    def __iter__(self):
        for line in self._handle:
            self._counter["bytes"] += len(line)
            yield line


def test_main_second_render_reads_only_the_appended_bytes(
    tmp_path, monkeypatch, capsys
):
    transcript = tmp_path / "session.jsonl"
    filler = _message_line(text="x" * 900)
    with open(transcript, "w", encoding="utf-8") as handle:
        handle.write(_entry_line(True, "claude-opus-5-5") + "\n")
        for _ in range(5 * 1024 * 1024 // len(filler)):
            handle.write(filler + "\n")
    assert transcript.stat().st_size > 5 * 1024 * 1024

    counter = {"bytes": 0}
    real_open = open

    def counting_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if str(path) == str(transcript):
            return _CountingFile(handle, counter)
        return handle

    monkeypatch.setattr(sl, "open", counting_open, raising=False)
    assert _render_advisor(monkeypatch, capsys, transcript) == "Opus 5.5"
    assert counter["bytes"] > 5 * 1024 * 1024  # the first render scans the file once

    counter["bytes"] = 0
    _append_lines(transcript, _message_line())
    assert _render_advisor(monkeypatch, capsys, transcript) == "Opus 5.5"
    assert 0 < counter["bytes"] < 64 * 1024


# --- debug dump — the payloads received, kept per session while the process runs in debug mode -

_DEBUG_PS = "claude --session-id sess-dbg --debug api,hooks\n"


def _debug_file(tmp_path, name="statusline", session_id="sess-dbg"):
    return (
        tmp_path
        / "home"
        / ".claude"
        / "debug"
        / f"{session_id}__pc-statusline__{name}.json"
    )


def _debug_payload(cost=1.5):
    return {
        **_RICH_PAYLOAD,
        "session_id": "sess-dbg",
        "cost": {"total_cost_usd": cost, "total_duration_ms": 0},
    }


def test_main_with_debug_flag_writes_the_payload_as_received(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(_common, "_ps", lambda *_args: _DEBUG_PS)
    _render_through_main(monkeypatch, capsys, _debug_payload(), 160)
    stored = json.loads(_debug_file(tmp_path).read_text(encoding="utf-8"))
    assert stored == [_debug_payload()]


def test_main_without_debug_flag_writes_nothing_to_the_debug_dir(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        _common, "_ps", lambda *_args: "claude --session-id sess-dbg --advisor opus\n"
    )
    _render_through_main(monkeypatch, capsys, _debug_payload(), 160)
    assert not (tmp_path / "home" / ".claude" / "debug").exists()


def test_main_debug_file_keeps_the_last_five_distinct_payloads_oldest_first(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(_common, "_ps", lambda *_args: _DEBUG_PS)
    costs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
    for cost in costs:
        _render_through_main(monkeypatch, capsys, _debug_payload(cost), 160)
    stored = json.loads(_debug_file(tmp_path).read_text(encoding="utf-8"))
    assert [p["cost"]["total_cost_usd"] for p in stored] == costs[-5:]


def test_main_identical_payload_does_not_rewrite_the_debug_file(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(_common, "_ps", lambda *_args: _DEBUG_PS)
    _render_through_main(monkeypatch, capsys, _debug_payload(), 160)
    path = _debug_file(tmp_path)
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    _render_through_main(monkeypatch, capsys, _debug_payload(), 160)
    assert path.stat().st_mtime_ns == 1_000_000_000
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 1


def test_main_unwritable_debug_dir_leaves_the_output_unchanged(
    tmp_path, monkeypatch, capsys
):
    _fixed_now_18h(monkeypatch)
    expected = _render_through_main(monkeypatch, capsys, _debug_payload(), 160)

    (tmp_path / "statusline-advisor-sess-dbg.json").unlink(missing_ok=True)
    blocker = tmp_path / "home" / ".claude" / "debug"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("a file where the directory should be", encoding="utf-8")
    monkeypatch.setattr(_common, "_ps", lambda *_args: _DEBUG_PS)
    assert _render_through_main(monkeypatch, capsys, _debug_payload(), 160) == expected
    assert blocker.is_file()


def test_main_reads_the_process_command_line_once_for_both_flags(
    tmp_path, monkeypatch, capsys
):
    calls = []

    def counting_ps(*args):
        calls.append(args)
        return "claude --session-id sess-dbg --advisor opus --debug\n"

    monkeypatch.setattr(_common, "_ps", counting_ps)
    for cost in (1.0, 2.0):
        out = _render_through_main(monkeypatch, capsys, _debug_payload(cost), 160)
        assert any("🧐 Opus" in ln for ln in out)
    assert len(calls) == 1
    assert _debug_file(tmp_path).exists()


def test_main_never_writes_the_command_line_into_the_debug_dump(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        _common,
        "_ps",
        lambda *_args: f"claude --session-id sess-dbg --debug {_CANARY_ARG}\n",
    )
    _render_through_main(monkeypatch, capsys, _debug_payload(), 160)
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert _debug_file(tmp_path) in written
    for path in written:
        assert _CANARY not in path.read_text(encoding="utf-8")
