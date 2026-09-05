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

import subprocess
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


# --- Headroom net indicator -----------------------------------------------------------------

import pytest as _pytest  # noqa: E402


def _stats(saved, lost, before, busts=0):
    """A minimal `/stats` payload shaped like the proxy's, keyed only where the code reads.

    `compression_vs_cache` nests under `prefix_cache` in the real payload (verified live against a
    running proxy 2026-09-04) — not at the top level. Getting this nesting wrong here would hide a
    wrong read path in the code the same way it did in production: a lookup that never resolves
    falls back through `or 0` on both sides, which reads as "nothing to report" rather than as an
    error, so nothing fails loudly.
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
    monkeypatch.setattr(sl, "_headroom_stats", lambda: None)


def test_headroom_net_is_savings_minus_cache_busts():
    assert sl._headroom_net(_stats(101_634, 388_415, 15_658_746, busts=4)) == (
        -286_781,
        _pytest.approx(1.8317, rel=1e-3),
        4,
    )


def test_headroom_net_is_positive_when_compression_outweighs_busts():
    net, percent, busts = sl._headroom_net(_stats(1_400_000, 64_000, 11_000_000))
    assert net == 1_336_000
    assert percent == _pytest.approx(12.145, rel=1e-3)
    assert busts == 0


def test_headroom_net_none_when_proxy_is_unreachable():
    assert sl._headroom_net(None) is None


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
def test_headroom_net_none_on_incomplete_or_poisoned_payload(payload):
    assert sl._headroom_net(payload) is None


def test_headroom_net_none_when_it_exactly_breaks_even():
    # No line beats a line saying "0" — the reader learns nothing from a wash.
    assert sl._headroom_net(_stats(500, 500, 1000, busts=1)) is None


def test_fmt_headroom_line_reads_as_a_gain_when_positive():
    line = _strip_ansi(sl._fmt_headroom_line(1_336_000, 12.145, 0))
    assert "🤑" in line and "12%" in line and "1336k tokens saved" in line
    assert "lost" not in line and "bust" not in line


def test_fmt_headroom_line_reads_as_a_loss_when_negative():
    line = _strip_ansi(sl._fmt_headroom_line(-286_781, 1.8317, 4))
    assert "💸" in line and "1.8%" in line and "286k tokens lost" in line
    assert "4 cache busts" in line


def test_fmt_headroom_line_singularises_a_lone_bust():
    assert "1 cache bust" in _strip_ansi(sl._fmt_headroom_line(-10_000, 1.0, 1))
    assert "1 cache busts" not in _strip_ansi(sl._fmt_headroom_line(-10_000, 1.0, 1))


def test_build_lines_renders_the_headroom_row_when_the_proxy_answers(monkeypatch):
    monkeypatch.setattr(
        sl, "_headroom_stats", lambda: _stats(1_400_000, 64_000, 11_000_000)
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
    # its own row, never appended to the context-window row
    assert not any("Context window" in ln and "Headroom" in ln for ln in stripped)


def test_build_lines_omits_the_headroom_row_when_the_proxy_is_down():
    stripped = [
        _strip_ansi(ln) for ln in sl.build_lines({"cost": {"total_duration_ms": 0}})
    ]
    assert not any("Headroom" in ln for ln in stripped)


# --- build_lines() end-to-end oracles -------------------------------------------------------


def _strip_ansi(line: str) -> str:
    import re

    line = re.sub(r"\033\[[0-9;]*m", "", line)  # SGR color codes
    return re.sub(r"\033\]8;;[^\033]*\033\\", "", line)  # OSC 8 hyperlink wrapper


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


# --- _repo_branch — repo/branch segment (CLAUDE_PROJECT_DIR is often empty in Claude Code,
# this is the one place a session can see which repo/branch it is sitting in) ------------------


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
    assert sl._repo_branch(str(repo)) == "my-repo @ main"


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
    assert sl._repo_branch(str(nested)) == "my-repo @ main"


def test_repo_branch_truncates_a_long_combo_with_an_ellipsis(tmp_path):
    repo = tmp_path / "pinky-agentic-plugins"
    repo.mkdir()
    _git("init", "-q", "-b", "feature/statusline-repo-branch", cwd=repo)
    _git("config", "user.email", "t@t.t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "x", cwd=repo)
    result = sl._repo_branch(str(repo))
    assert len(result) == sl._REPO_BRANCH_MAX_LEN
    assert result.endswith("…")


def test_repo_branch_none_outside_a_repo(tmp_path):
    assert sl._repo_branch(str(tmp_path)) is None


def test_repo_branch_none_on_falsy_input():
    assert sl._repo_branch(None) is None
    assert sl._repo_branch("") is None


def test_build_lines_row2_shows_repo_branch_on_the_right(tmp_path):
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
    assert "📁 pinky-lib @ feature/x" in stripped[4]


def test_build_lines_row2_omits_repo_branch_outside_a_repo(tmp_path):
    data = {"workspace": {"current_dir": str(tmp_path)}}
    stripped = [_strip_ansi(ln) for ln in sl.build_lines(data)]
    assert "📁" not in stripped[4]


def test_build_lines_row2_repo_branch_coexists_with_subagent_count(
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
    assert "📁 pinky-lib @ main" in stripped[4]
    assert "2 subagents" in stripped[4]


# --- _hyperlink / short session id -----------------------------------------------------------


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
