#!/usr/bin/env python3
"""Tests for plugins/pc-models/mcp/model_tier_probe.py — run with: pytest tests/test_model_tier_probe.py"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-models" / "mcp"))

import model_adversarial as ma
import model_tier_probe as tp

HAIKU, SONNET, OPUS = tp.FIXTURES


def _entry(name, provider="prov", **extra):
    return {
        "name": name,
        "kind": "cli",
        "provider": provider,
        "model": name,
        "capacity_tier": "sonnet",
        "tools": True,
        **extra,
    }


def _hit(fixture, remarks=0):
    """A reply naming the planted bug, followed by `remarks` findings elsewhere."""
    lines = [f"{fixture.file}:{fixture.line}: the planted bug"]
    lines += [f"unrelated.py:{n}: a remark" for n in range(1, remarks + 1)]
    return "\n".join(lines)


def _miss(fixture):
    return f"{fixture.file}:{fixture.line + 100}: the wrong line"


def _scripted(monkeypatch, replies, seen=None):
    """A fake `call_cli`: `replies[level]` is consumed one reply per call, for the level whose
    fixture files the prompt lists."""
    queues = {level: list(r) for level, r in replies.items()}

    def fake(entry, prompt, timeout, tools=None, env=None, events=None):
        if seen is not None:
            seen.append((entry["name"], timeout, tools))
        fixture = next(f for f in tp.FIXTURES if f.file in prompt and f.level in queues)
        return queues[fixture.level].pop(0)

    monkeypatch.setattr(ma, "call_cli", fake)


@pytest.mark.parametrize("fixture", tp.FIXTURES, ids=lambda f: f.level)
def test_fixture_has_one_planted_bug_at_the_expected_line(fixture):
    marked = [
        n
        for n, line in enumerate(fixture.sources[fixture.file].splitlines(), 1)
        if fixture.marker in line
    ]
    assert marked == [fixture.line]


@pytest.mark.parametrize("fixture", tp.FIXTURES, ids=lambda f: f.level)
def test_criterion_accepts_the_reference_and_refuses_another_file(fixture):
    assert tp.names_the_bug(fixture, _hit(fixture))
    assert not tp.names_the_bug(fixture, f"other.py:{fixture.line}: the bug")
    assert not tp.names_the_bug(fixture, _miss(fixture))
    assert not tp.names_the_bug(fixture, "")


def test_criterion_accepts_the_line_wording_and_a_path_prefix():
    assert tp.names_the_bug(HAIKU, f"- `src/{HAIKU.file}` line {HAIKU.line}: x")
    assert tp.names_the_bug(HAIKU, f"{HAIKU.file}, line {HAIKU.line}: x")
    assert not tp.names_the_bug(HAIKU, f"x{HAIKU.file}:{HAIKU.line + 1}")


def test_tier_is_the_highest_level_found_twice(monkeypatch):
    _scripted(
        monkeypatch,
        {
            "haiku": [_hit(HAIKU)] * 2,
            "sonnet": [_hit(SONNET)] * 2,
            "opus": [_hit(OPUS), _miss(OPUS)],
        },
    )
    result = tp.probe_entry(_entry("m"), {})
    assert result.tier == "sonnet"
    assert [(lv.level, lv.hits) for lv in result.levels] == [
        ("haiku", 2),
        ("sonnet", 2),
        ("opus", 1),
    ]


def test_all_three_levels_found_twice_is_opus(monkeypatch):
    _scripted(
        monkeypatch,
        {f.level: [_hit(f)] * 2 for f in tp.FIXTURES},
    )
    assert tp.probe_entry(_entry("m"), {}).tier == "opus"


def test_haiku_bug_found_once_is_sous_haiku_even_if_higher_levels_pass(monkeypatch):
    _scripted(
        monkeypatch,
        {
            "haiku": [_hit(HAIKU), _miss(HAIKU)],
            "sonnet": [_hit(SONNET)] * 2,
            "opus": [_hit(OPUS)] * 2,
        },
    )
    assert tp.probe_entry(_entry("m"), {}).tier == "sous haiku"


def test_off_target_remarks_are_counted_per_reply_and_leave_the_tier_alone(
    monkeypatch,
):
    replies = {
        "haiku": [_hit(HAIKU)] * 2,
        "sonnet": [_hit(SONNET, remarks=3), _hit(SONNET)],
        "opus": [_miss(OPUS)] * 2,
    }
    _scripted(monkeypatch, replies)
    noisy = tp.probe_entry(_entry("m"), {})
    sonnet = noisy.levels[1]
    assert sonnet.off_target == [3, 0]
    assert noisy.tier == "sonnet"
    _scripted(monkeypatch, {**replies, "sonnet": [_hit(SONNET)] * 2})
    assert tp.probe_entry(_entry("m"), {}).tier == noisy.tier


def test_a_failed_call_is_a_miss_and_is_counted_as_an_error(monkeypatch):
    def fake(entry, prompt, timeout, tools=None, env=None, events=None):
        raise subprocess.CalledProcessError(1, "pi", "", "429 Too Many Requests")

    monkeypatch.setattr(ma, "call_cli", fake)
    result = tp.probe_entry(_entry("m"), {})
    assert (result.tier, result.errors) == ("sous haiku", 6)


def test_each_call_gets_the_read_grep_tools_and_twenty_seconds_per_step(monkeypatch):
    seen = []
    _scripted(monkeypatch, {f.level: [""] * 4 for f in tp.FIXTURES}, seen)
    tp.probe_entry(_entry("m"), {})
    tp.probe_entry(_entry("own", timeout=90), {})
    assert len(seen) == 12
    assert all(tools == ["read", "grep"] for _, _, tools in seen)
    assert {t for n, t, _ in seen if n == "m"} == {
        tp.STEP_BUDGET * ma.PROBE_STEP_SECONDS
    }
    assert {t for n, t, _ in seen if n == "own"} == {90}


def test_prompt_lists_the_files_the_model_can_read(monkeypatch):
    prompts = []

    def fake(entry, prompt, timeout, tools=None, env=None, events=None):
        prompts.append(prompt)
        return ""

    monkeypatch.setattr(ma, "call_cli", fake)
    tp.probe_entry(_entry("m"), {})
    assert all("change.diff" in p for p in prompts)
    assert all(f.file in p for f, p in zip(tp.FIXTURES, prompts[::2]))


def test_entries_of_one_provider_run_one_after_another(monkeypatch):
    spans = {}
    lock = threading.Lock()

    def fake(entry, prompt, timeout, tools=None, env=None, events=None):
        start = time.monotonic()
        time.sleep(0.02)
        with lock:
            spans.setdefault(entry["name"], []).append((start, time.monotonic()))
        return ""

    monkeypatch.setattr(ma, "call_cli", fake)
    tp.probe_pool([_entry("a1", "pa"), _entry("a2", "pa"), _entry("b1", "pb")])
    a1, a2, b1 = (spans[n] for n in ("a1", "a2", "b1"))
    assert max(e for _, e in a1) <= min(s for s, _ in a2)
    assert min(s for s, _ in b1) < max(e for _, e in a1)


def test_only_tools_true_cli_entries_are_probed_the_others_are_skipped(monkeypatch):
    calls = []

    def fake(entry, prompt, timeout, tools=None, env=None, events=None):
        calls.append(entry["name"])
        return ""

    monkeypatch.setattr(ma, "call_cli", fake)
    pool = [
        _entry("yes"),
        _entry("no", tools=False),
        _entry("unset", tools=None),
        {"name": "local", "kind": "offline", "tools": True},
    ]
    results, skipped = tp.probe_pool(pool)
    assert [r.name for r in results] == ["yes"]
    assert set(calls) == {"yes"}
    assert skipped == [
        ("no", "tools: false"),
        ("unset", "tools: not true"),
        ("local", "not kind: cli"),
    ]


def test_render_is_the_whole_table():
    result = tp.EntryTier(
        "m",
        "sonnet",
        [
            tp.LevelResult("haiku", 2, [0, 1]),
            tp.LevelResult("sonnet", 2, [3, 0]),
            tp.LevelResult("opus", 1, [0, 0]),
        ],
        errors=1,
    )
    assert tp.render([result], [tp.Skipped("off", "tools: false")]) == (
        "entry  tier    haiku  sonnet  opus  remarks  errors\n"
        "m      sonnet  2/2    2/2     1/2         4       1\n"
        "\n"
        "Proposed only — nothing is written; edit `capacity_tier` in the pool yourself.\n"
        "skipped: off (tools: false)"
    )


def test_main_writes_no_pool_configuration(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    settings = project / ".claude" / "settings.pinky.json"
    settings.write_text(json.dumps({"model_pool": [_entry("m")]}))
    before = settings.read_bytes()
    tree = sorted(p.relative_to(project) for p in project.rglob("*"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    _scripted(monkeypatch, {f.level: [_hit(f)] * 2 for f in tp.FIXTURES})
    assert tp.main([]) == 0
    assert "opus" in capsys.readouterr().out
    assert settings.read_bytes() == before
    assert sorted(p.relative_to(project) for p in project.rglob("*")) == tree


def test_main_name_filters_and_an_unknown_name_exits_2(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.pinky.json").write_text(
        json.dumps({"model_pool": [_entry("m"), _entry("n")]})
    )
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    seen = []
    _scripted(monkeypatch, {f.level: [""] * 2 for f in tp.FIXTURES}, seen)
    assert tp.main(["--name", "n"]) == 0
    assert {name for name, _, _ in seen} == {"n"}
    assert tp.main(["--name", "ghost"]) == 2
    assert "ghost" in capsys.readouterr().err
