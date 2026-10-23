"""Tests for `pi_live.py`: the run directory the pi-view mod names, and the view files it reads."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-models" / "mcp"))
import pi_live  # noqa: E402


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path / "cfg" / "pinky" / "plugin-state" / "pc-models" / "pi-runs"


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def ev(kind: str, **fields) -> str:
    return json.dumps({"type": kind, **fields}) + "\n"


def delta(kind: str, text: str) -> str:
    return ev("message_update", assistantMessageEvent={"type": kind, "delta": text})


def test_open_run_creates_a_private_directory(runs):
    run = pi_live.open_run(str(runs / "toolu_01AbC-d_9"))
    assert run == runs / "toolu_01AbC-d_9"
    assert run.is_dir()
    assert run.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "name",
    ["", "..", "a/b", "x y", "a" * 129, "toolu;rm"],
)
def test_open_run_refuses_a_name_outside_the_pattern(runs, name):
    assert pi_live.open_run(str(runs / name) if name else "") is None


def test_open_run_refuses_a_path_outside_the_runs_directory(runs, tmp_path):
    assert pi_live.open_run(str(tmp_path / "elsewhere" / "toolu_1")) is None
    assert pi_live.open_run(str(runs / "toolu_1" / "deeper")) is None
    assert not (tmp_path / "elsewhere").exists()


def test_open_run_refuses_an_existing_directory_or_link(runs, tmp_path):
    runs.mkdir(parents=True)
    (runs / "taken").mkdir()
    (runs / "link").symlink_to(tmp_path)
    assert pi_live.open_run(str(runs / "taken")) is None
    assert pi_live.open_run(str(runs / "link")) is None


def test_open_run_with_no_value_is_none(runs):
    assert pi_live.open_run(None) is None
    assert not runs.exists()


def test_first_access_removes_the_legacy_pc_models_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    legacy = tmp_path / "cfg" / "pc-models" / "pi-runs" / "old-run"
    legacy.mkdir(parents=True)
    assert pi_live.runs_dir() == (
        tmp_path / "cfg" / "pinky" / "plugin-state" / "pc-models" / "pi-runs"
    )
    assert not (tmp_path / "cfg" / "pc-models").exists()


def test_no_legacy_dir_is_created_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    pi_live.runs_dir()
    assert not (tmp_path / "cfg" / "pc-models").exists()


def test_open_run_prunes_runs_older_than_a_day(runs):
    runs.mkdir(parents=True)
    old, fresh = runs / "old", runs / "fresh"
    old.mkdir()
    fresh.mkdir()
    (old / "view-1.txt").write_text("x")
    past = time.time() - pi_live.MAX_AGE_S - 10
    os.utime(old, (past, past))
    pi_live.open_run(str(runs / "new"))
    assert sorted(p.name for p in runs.iterdir()) == ["fresh", "new"]


def test_view_renders_attempts_thinking_text_and_end(runs):
    run = pi_live.open_run(str(runs / "t1"))
    clock = Clock()
    view = pi_live.LiveView(run, 1, "adversarial", clock=clock)
    view.write(ev("pool_attempt", entry="m1", attempt=1))
    clock.t = 101.0
    view.write(ev("message_update", assistantMessageEvent={"type": "thinking_start"}))
    view.write(delta("thinking_delta", "abc"))
    view.write(delta("thinking_delta", "de"))
    clock.t = 102.5
    view.write(delta("text_delta", "Looks "))
    view.write(delta("text_delta", "fine."))
    view.write(ev("message_update", assistantMessageEvent={"type": "text_end"}))
    view.write(ev("pool_result", entry="m1", attempt=1, outcome="answered"))
    view.close()
    assert (run / "view-1.txt").read_text() == (
        "adversarial\n"
        "[  0.0s] == m1 (attempt 1) ==\n"
        "[  1.0s] thinking · 5 chars\n"
        "[  2.5s] text: Looks fine.\n"
        "[  2.5s] -> m1 attempt 1: answered\n"
    )


def test_view_keeps_the_last_lines_and_the_tail_of_a_long_one(runs):
    run = pi_live.open_run(str(runs / "t2"))
    view = pi_live.LiveView(run, 2, "slot", clock=Clock())
    view.write(ev("pool_attempt", entry="m1", attempt=1))
    for i in range(pi_live.KEEP_LINES):
        view.write(
            ev("tool_execution_start", toolName="read", args={"file_path": f"/f{i}"})
        )
    view.write(delta("text_delta", "x" * 500 + "END"))
    view.close()
    lines = (run / "view-2.txt").read_text().splitlines()
    assert len(lines) == pi_live.KEEP_LINES
    assert lines[0] == "slot"
    assert lines[1] == "[  0.0s] == m1 (attempt 1) =="
    assert lines[-1].endswith("END") and len(lines[-1]) == pi_live.LINE_CHARS


def test_view_keeps_the_attempt_line_under_the_header_once_the_stream_scrolls(runs):
    run = pi_live.open_run(str(runs / "t6"))
    view = pi_live.LiveView(run, 1, "adversarial", clock=Clock())
    view.write(ev("pool_attempt", entry="nvidia-minimax-m3.1-flash", attempt=1))
    for i in range(pi_live.KEEP_LINES + 2):
        view.write(
            ev("tool_execution_start", toolName="read", args={"file_path": f"/f{i}"})
        )
        view.write(
            ev("tool_execution_end", toolName="read", isError=False, durationMs=3)
        )
    view.close()
    last_pairs = range(
        pi_live.KEEP_LINES + 2 - (pi_live.KEEP_LINES - 2) // 2, pi_live.KEEP_LINES + 2
    )
    assert (run / "view-1.txt").read_text() == (
        "adversarial\n"
        "[  0.0s] == nvidia-minimax-m3.1-flash (attempt 1) ==\n"
        + "".join(
            f"[  0.0s] tool read(file_path=/f{i})\n[  0.0s]   <- read ok 3 ms\n"
            for i in last_pairs
        )
    )


def test_view_pins_the_latest_attempt_once_several_scroll_past(runs):
    run = pi_live.open_run(str(runs / "t7"))
    view = pi_live.LiveView(run, 1, "adversarial", clock=Clock())
    for attempt in (1, 2):
        view.write(ev("pool_attempt", entry="m1", attempt=attempt))
        for i in range(pi_live.KEEP_LINES + 1):
            view.write(
                ev("tool_execution_start", toolName="grep", args={"pattern": f"p{i}"})
            )
    view.close()
    assert (run / "view-1.txt").read_text() == (
        "adversarial\n"
        "[  0.0s] == m1 (attempt 2) ==\n"
        + "".join(
            f"[  0.0s] tool grep(pattern=p{i})\n"
            for i in range(3, pi_live.KEEP_LINES + 1)
        )
    )


def test_view_takes_a_line_split_across_writes(runs):
    run = pi_live.open_run(str(runs / "t3"))
    view = pi_live.LiveView(run, 1, "h", clock=Clock())
    line = ev("pool_attempt", entry="m1", attempt=2)
    view.write(line[:10])
    view.write(line[10:])
    view.close()
    assert (run / "view-1.txt").read_text() == "h\n[  0.0s] == m1 (attempt 2) ==\n"


def test_view_writes_at_most_every_interval_until_closed(runs):
    run = pi_live.open_run(str(runs / "t4"))
    clock = Clock()
    view = pi_live.LiveView(run, 1, "h", clock=clock)
    view.write(ev("pool_attempt", entry="m1", attempt=1))
    assert (run / "view-1.txt").read_text() == "h\n"
    clock.t += pi_live.WRITE_EVERY_S
    view.flush()
    assert (run / "view-1.txt").read_text() == "h\n[  0.0s] == m1 (attempt 1) ==\n"


def test_progress_view_counts_characters_never_shows_them(runs):
    run = pi_live.open_run(str(runs / "t5"))
    progress = pi_live.ProgressView(run, "offline · mlx-model", clock=Clock())
    progress("SECRET ")
    progress("BODY")
    progress.close()
    assert (
        run / "view-1.txt"
    ).read_text() == "offline · mlx-model\n11 chars received\n[  0.0s] pool end\n"
