"""subagentStatusLine — one custom row per subagent in the agents panel.

Companion to test_statusline.py, same oracle strategy (tests.md § generator oracle): unit
oracles on the small pure helpers, then end-to-end oracles on `render_task()` — the pure entry
point `main()`'s per-task loop calls, for a testable seam. A single malformed task's
`startTime`/`tokenSamples` being the wrong TYPE must not blank every other agent's row —
`main()` isolates each task's render in a try/except, and `render_task()` itself falls back to the
dim `⏱ —` placeholder on a type mismatch instead of trusting the schema blindly.
"""

import json
import sys
from pathlib import Path

HOOKS = Path(__file__).parent.parent / "plugins" / "pc-statusline" / "scripts"
sys.path.insert(0, str(HOOKS))

import subagent_statusline as sal  # noqa: E402


# --- small pure helpers -------------------------------------------------------------------


def test_short_model_oracle():
    assert sal._short_model("claude-haiku-4-5-20251001") == "Haiku 4.5"
    assert sal._short_model("claude-sonnet-5") == "Sonnet 5"
    assert sal._short_model(None) == "?"
    assert sal._short_model("") == "?"


def test_visible_len_ignores_ansi_and_counts_emoji_as_wide():
    assert sal._visible_len(f"{sal.CYAN}ab{sal.RESET}") == 2
    assert sal._visible_len("🤖") == 2
    assert sal._visible_len("ab") == 2


def test_visible_len_treats_a_hyperlink_as_zero_width():
    assert sal._visible_len("\033]8;;file:///some/path\033\\ab\033]8;;\033\\") == 2


def test_format_duration_oracle():
    assert sal._format_duration(0, now_ms=42_000) == "42s"
    assert sal._format_duration(0, now_ms=102_000) == "1m42s"
    assert sal._format_duration(0, now_ms=3_900_000) == "1h05m"


def test_format_duration_tolerates_clock_skew_and_non_numbers():
    assert sal._format_duration(10_000, now_ms=5_000) == "0s"
    assert sal._format_duration("not-a-number") is None
    assert sal._format_duration(None) is None


def test_sparkline_one_block_per_interval_scaled_on_the_peak():
    assert sal._sparkline([0, 100, 300, 300, 50, 450]) == "▂▄▁▁█"


def test_sparkline_restarts_from_the_value_after_a_counter_reset():
    # (10 -> 5) : compteur repassé à zéro, le delta est la valeur elle-même
    assert sal._sparkline([0, 10, 5, 10]) == "█▄▄"


def test_sparkline_flat_activity_is_the_lowest_block():
    assert sal._sparkline([1, 1, 1]) == "▁▁"


def test_sparkline_needs_two_numeric_samples():
    assert sal._sparkline([5]) is None
    assert sal._sparkline("not-a-list") is None
    assert sal._sparkline(["a", "b"]) is None


# --- render_task() oracles -----------------------------------------------------------------


def _strip_ansi(s: str) -> str:
    import re

    return re.sub(r"\033\[[0-9;]*m", "", s)


def test_render_task_returns_none_without_an_id():
    assert sal.render_task({}, columns=80) is None
    assert sal.render_task({"name": "x"}, columns=80) is None


_NOW_S = 1_000_000.0


def _running_task(**overrides) -> dict:
    """A task that started 102 s before `_NOW_S`, with samples of its generated-token counter."""
    task = {
        "id": "t1",
        "name": "worker",
        "status": "running",
        "model": "claude-haiku-4-5",
        "effort": "high",
        "startTime": _NOW_S * 1000 - 102_000,
        "tokenSamples": [0, 100, 300, 300, 50, 450],
    }
    return {**task, **overrides}


def test_render_task_oracle_full_task_readable_content(monkeypatch):
    monkeypatch.setattr(sal.time, "time", lambda: _NOW_S)
    line = sal.render_task(_running_task(), columns=80)
    assert line["id"] == "t1"
    assert _strip_ansi(line["content"]) == (
        "Running ⚙️ worker · 🤖 Haiku 4.5 · 💪 : high · ⏱ 1m42s ▂▄▁▁█"
    )


def test_render_task_unresolved_model_and_start_time_show_dim_placeholders():
    """The model and the start time are absent while the task is pending (documented harness
    behaviour, not a malformed payload) — must degrade quietly, not guess."""
    task = {"id": "t1", "name": "worker", "status": "pending"}
    stripped = _strip_ansi(sal.render_task(task, columns=80)["content"])
    assert stripped == "Pending ⏳ worker · 🤖 ? · 💪 : — · ⏱ —"


def test_render_task_shows_no_duration_once_the_task_is_not_running(monkeypatch):
    monkeypatch.setattr(sal.time, "time", lambda: _NOW_S)
    stripped = _strip_ansi(
        sal.render_task(_running_task(status="completed"), columns=80)["content"]
    )
    assert stripped.endswith("⏱ —")
    assert "▂" not in stripped


def test_render_task_tolerates_wrong_typed_duration_and_samples():
    """startTime/tokenSamples holding the wrong TYPE (a string, from a misbehaving upstream)
    degrade to the placeholder instead of raising."""
    task = _running_task(startTime="not-a-number", tokenSamples="not-a-list")
    stripped = _strip_ansi(sal.render_task(task, columns=80)["content"])
    assert stripped.endswith("⏱ —")


def test_render_task_truncates_a_long_name_to_the_column_budget():
    """Oracle on the exact truncation slice — the fixed-cost segments (status emoji, model,
    duration placeholder) leave a 13-column name budget at columns=30 for this task shape, so a
    50-char name must come back as exactly 12 chars + the ellipsis."""
    task = {"id": "t1", "name": "A" * 50, "status": ""}
    stripped = _strip_ansi(sal.render_task(task, columns=30)["content"])
    assert stripped == " ▪️ " + "A" * 12 + "… · 🤖 ? · ⏱ —"


def test_render_task_leaves_a_short_name_untouched():
    task = {"id": "t1", "name": "ok", "status": "completed"}
    stripped = _strip_ansi(sal.render_task(task, columns=80)["content"])
    assert "ok" in stripped
    assert "…" not in stripped


def test_render_task_unknown_status_falls_back_to_default_emoji():
    task = {"id": "t1", "name": "worker", "status": "some-future-status"}
    stripped = _strip_ansi(sal.render_task(task, columns=80)["content"])
    assert "▪️" in stripped


# --- main() end-to-end: one bad task must not blank the others -----------------------------


def test_render_task_tolerates_a_dict_where_a_number_was_expected():
    """startTime/tokenSamples holding a wrong-shaped VALUE (not just a wrong scalar type) must
    also degrade via the isinstance guard, not just plain strings."""
    task = _running_task(startTime={"x": 1}, tokenSamples={"x": 1})
    stripped = _strip_ansi(sal.render_task(task, columns=80)["content"])
    assert stripped.endswith("⏱ —")


def test_main_skips_a_non_dict_task_but_still_renders_the_rest():
    """The real invariant: main() only calls render_task() on dict-shaped tasks, and wraps that
    call in its own try/except — so one adversarial entry in the tasks array (not even a dict)
    degrades to Claude Code's default row for that slot instead of silencing every other agent's
    status on the same refresh tick."""
    import subprocess

    payload = {
        "columns": 80,
        "tasks": [
            {"id": "good-1", "name": "alpha", "status": "running"},
            "not-a-task-object",
            {"id": "good-2", "name": "gamma", "status": "completed"},
        ],
    }
    result = subprocess.run(
        [sys.executable, str(HOOKS / "subagent_statusline.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert result.returncode == 0
    out_ids = set()
    for line in result.stdout.splitlines():
        out_ids.add(json.loads(line)["id"])
    assert out_ids == {"good-1", "good-2"}


def test_main_never_crashes_on_adversarial_json_shapes():
    import subprocess

    adversarial_payloads = [
        "not even json {{{",
        "null",  # valid JSON, wrong top-level shape — not a dict
        "[]",
        json.dumps({"tasks": "not-a-list"}),
        json.dumps(
            {"tasks": [{"id": "t1"}]}
        ),  # minimal valid task, everything else absent
    ]
    for raw in adversarial_payloads:
        result = subprocess.run(
            [sys.executable, str(HOOKS / "subagent_statusline.py")],
            input=raw,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        assert result.returncode == 0, (
            f"subagent_statusline.py exited {result.returncode} on input {raw!r}\n{result.stderr}"
        )


# --- agent type, read from the subagent metadata -------------------------------------------


def _write_meta(tmp_path: Path, task_id: str, body: str) -> Path:
    transcript = tmp_path / "session.jsonl"
    meta_dir = tmp_path / "session" / "subagents"
    meta_dir.mkdir(parents=True)
    (meta_dir / f"agent-{task_id}.meta.json").write_text(body, encoding="utf-8")
    return transcript


def test_agent_type_reads_the_meta_json_next_to_the_transcript(tmp_path):
    transcript = _write_meta(
        tmp_path, "t1", json.dumps({"agentType": "general-purpose"})
    )
    assert sal._agent_type(str(transcript), "t1") == "general-purpose"


def test_agent_type_is_none_when_the_meta_json_is_missing_or_unreadable(tmp_path):
    transcript = _write_meta(tmp_path, "t1", "not json {")
    assert sal._agent_type(str(transcript), "t1") is None
    assert sal._agent_type(str(transcript), "absent") is None
    assert sal._agent_type(None, "t1") is None


def test_render_task_oracle_with_agent_type(monkeypatch):
    monkeypatch.setattr(sal.time, "time", lambda: _NOW_S)
    stripped = _strip_ansi(
        sal.render_task(
            _running_task(description="Fix flaky test"),
            columns=120,
            agent_type="general-purpose",
        )["content"]
    )
    assert stripped == (
        "Running ⚙️ worker · 🧬 general-purpose · 🤖 Haiku 4.5 · 💪 : high · "
        "⏱ 1m42s ▂▄▁▁█"
    )


def test_render_task_sacrifices_the_agent_type_then_the_effort_before_the_name(
    monkeypatch,
):
    """The row loses, in order, the agent type, the effort, and only then part of the name
    (status, model and duration always stay)."""
    monkeypatch.setattr(sal.time, "time", lambda: _NOW_S)
    task = _running_task(name="x" * 20)

    def render(columns):
        return _strip_ansi(
            sal.render_task(task, columns=columns, agent_type="general-purpose")[
                "content"
            ]
        )

    assert render(100) == (
        "Running ⚙️ " + "x" * 20 + " · 🧬 general-purpose · 🤖 Haiku 4.5 · 💪 : high · "
        "⏱ 1m42s ▂▄▁▁█"
    )
    assert render(75) == (
        "Running ⚙️ " + "x" * 20 + " · 🤖 Haiku 4.5 · 💪 : high · ⏱ 1m42s ▂▄▁▁█"
    )
    assert render(70) == "Running ⚙️ " + "x" * 20 + " · 🤖 Haiku 4.5 · ⏱ 1m42s ▂▄▁▁█"
    assert render(60) == "Running ⚙️ " + "x" * 16 + "… · 🤖 Haiku 4.5 · ⏱ 1m42s ▂▄▁▁█"


def test_main_shows_the_agent_type_from_meta_json(tmp_path):
    import subprocess

    transcript = _write_meta(
        tmp_path, "t1", json.dumps({"agentType": "general-purpose"})
    )
    payload = {
        "columns": 120,
        "transcript_path": str(transcript),
        "tasks": [{"id": "t1", "description": "Fix flaky test", "status": "running"}],
    }
    result = subprocess.run(
        [sys.executable, str(HOOKS / "subagent_statusline.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert "🧬 general-purpose" in _strip_ansi(json.loads(result.stdout)["content"])
