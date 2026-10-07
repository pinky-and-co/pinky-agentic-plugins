#!/usr/bin/env python3
"""Tests for `hooks/pii_guard.py` — the PreToolUse/Read guard.

`decide`/`run_hook`/`check` stay real; only boundaries are faked: the event via synthetic
JSON, the analyzer via monkeypatched `anonymize._build_analyzer`/`detect_findings`, the size
limit via the module's `MAX_BYTES`.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-privacy" / "mcp"))
sys.path.insert(
    0, str(Path(__file__).parent.parent / "plugins" / "pc-privacy" / "hooks")
)

import anonymize
import pii_guard

requires_model = pytest.mark.skipif(
    anonymize.preflight() is not None,
    reason="Presidio / fr_core_news_md not installed in this env",
)


def _csv(path: Path, rows: list[dict]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return path


def _read_event(path: Path) -> dict:
    return {"tool_name": "Read", "tool_input": {"file_path": str(path)}}


def _feed_event(monkeypatch, event: dict) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))


def test_read_on_config_or_binary_allows_silently_without_loading_the_model(
    tmp_path, monkeypatch, capsys
):
    """The path most `Read` calls take: config and binary targets produce no output and never
    pay the NLP model load."""
    loaded = []
    monkeypatch.setattr(anonymize, "_build_analyzer", lambda: loaded.append(1))
    conf = tmp_path / "app.yml"
    conf.write_text("email: jean@example.fr\n", encoding="utf-8")
    png = tmp_path / "img.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    for target in (conf, png):
        _feed_event(monkeypatch, _read_event(target))
        assert pii_guard.run_hook() == 0
        assert capsys.readouterr().out == ""
    assert loaded == []


def test_read_on_a_dataset_with_pii_asks_without_leaking_values(
    tmp_path, monkeypatch, capsys
):
    """A flagged dataset emits an `ask` decision whose reason carries column names and entity
    types only — never a sampled value."""
    data = _csv(
        tmp_path / "clients.csv",
        [{"contact": "jean.dupont@example.fr", "note": "x"} for _ in range(3)],
    )
    monkeypatch.setattr(
        anonymize,
        "detect_findings",
        lambda files, **kw: [
            anonymize.Finding("clients.csv", "contact", "detected EMAIL_ADDRESS (3/3)")
        ],
    )
    _feed_event(monkeypatch, _read_event(data))
    assert pii_guard.run_hook() == 0

    payload = json.loads(capsys.readouterr().out)
    decision = payload["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
    reason = decision["permissionDecisionReason"]
    assert "contact" in reason
    assert "EMAIL_ADDRESS" in reason
    assert "jean.dupont@example.fr" not in reason


def test_a_tool_other_than_read_is_ignored(tmp_path):
    """Only `Read` is guarded — any other tool name returns no decision."""
    event = {"tool_name": "Grep", "tool_input": {"file_path": str(tmp_path / "x.csv")}}
    assert pii_guard.decide(event, anonymize) is None


def test_missing_or_nonexistent_file_path_is_left_to_read_itself(tmp_path):
    """No `file_path`, or a path that does not exist: no output, `Read` produces its own
    error message."""
    assert pii_guard.decide({"tool_name": "Read", "tool_input": {}}, anonymize) is None
    gone = tmp_path / "gone.csv"
    assert (
        pii_guard.decide(
            {"tool_name": "Read", "tool_input": {"file_path": str(gone)}}, anonymize
        )
        is None
    )


def test_an_internal_failure_fails_open(tmp_path, monkeypatch, capsys):
    """A guard bug must never block the tool call: a raised detector surfaces on stderr,
    exits 0, and prints nothing on stdout."""
    data = _csv(tmp_path / "c.csv", [{"contact": "a@example.fr", "note": "x"}])

    def boom(files: list[str], **kw) -> None:
        raise RuntimeError("simulated guard bug")

    monkeypatch.setattr(anonymize, "detect_findings", boom)
    _feed_event(monkeypatch, _read_event(data))
    assert pii_guard.run_hook() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "disabled for this call" in captured.err


def test_an_oversize_file_asks_not_analyzed_without_scanning(tmp_path, monkeypatch):
    """Above the size limit the hook asks with an explicit "NOT analyzed" message and never
    runs the scan — not a silent allow, not a hang."""
    data = tmp_path / "big.csv"
    data.write_text("contact,note\n" + "user@example.fr,x\n" * 80_000, encoding="utf-8")
    monkeypatch.setattr(pii_guard, "MAX_BYTES", 1024 * 1024)
    scanned = []
    monkeypatch.setattr(
        anonymize, "detect_findings", lambda files, **kw: scanned.append(1)
    )

    decision = pii_guard.decide(_read_event(data), anonymize)

    assert decision is not None
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert "NOT analyzed" in reason
    assert scanned == []


@requires_model
def test_check_exits_zero_when_the_model_loads(capsys):
    """`--check` is the only way to notice a silently fail-open guard: it must report OK
    (exit 0) when the full analyzer actually builds."""
    assert pii_guard.check() == 0
    assert "OK" in capsys.readouterr().out


def test_check_exits_one_when_the_model_cannot_load(monkeypatch, capsys):
    """A model that cannot load makes `--check` exit 1 with the reason on stderr."""
    monkeypatch.setattr(
        anonymize,
        "_build_analyzer",
        lambda: (_ for _ in ()).throw(
            anonymize.AnonymizeError("simulated missing model")
        ),
    )
    assert pii_guard.check() == 1
    assert "NOT WORKING" in capsys.readouterr().err


def _prompt_event(text: str, cwd: Path) -> dict:
    return {"hook_event_name": "UserPromptSubmit", "prompt": text, "cwd": str(cwd)}


def _flag_csv(monkeypatch):
    monkeypatch.setattr(
        anonymize,
        "detect_findings",
        lambda files, **kw: [
            anonymize.Finding(
                Path(files[0]).name, "contact", "detected EMAIL_ADDRESS (3/3)"
            )
        ],
    )


def test_prompt_mentioning_a_pii_file_with_at_is_blocked_without_leaking_values(
    tmp_path, monkeypatch, capsys
):
    """`@file` never goes through `Read`, so the prompt hook is the only interception point: the
    message is blocked, the reason names columns and entity types only, and it says how to get
    the confirmation dialog back."""
    data = _csv(tmp_path / "clients.csv", [{"contact": "jean@example.fr"}] * 3)
    _flag_csv(monkeypatch)
    _feed_event(monkeypatch, _prompt_event(f"lis @{data} stp", tmp_path))
    assert pii_guard.run_hook() == 0

    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert "contact" in out["reason"]
    assert "EMAIL_ADDRESS" in out["reason"]
    assert "jean@example.fr" not in out["reason"]
    assert "without the @" in out["reason"]


def test_prompt_without_at_mentions_is_left_alone(tmp_path, monkeypatch, capsys):
    """A plain path (no `@`) goes through `Read`, where the ask dialog applies."""
    data = _csv(tmp_path / "clients.csv", [{"contact": "jean@example.fr"}] * 3)
    scanned = []
    monkeypatch.setattr(
        anonymize, "detect_findings", lambda files, **kw: scanned.append(files) or []
    )
    _feed_event(monkeypatch, _prompt_event(f"lis {data} stp", tmp_path))
    assert pii_guard.run_hook() == 0
    assert capsys.readouterr().out == ""
    assert scanned == []


def test_prompt_at_mention_of_a_clean_or_missing_file_or_a_dir_passes(
    tmp_path, monkeypatch, capsys
):
    """Config/code, a missing path, a directory and an email-like `a@b.fr` token never block."""
    conf = tmp_path / "app.yml"
    conf.write_text("k: v\n", encoding="utf-8")
    monkeypatch.setattr(
        anonymize, "_build_analyzer", lambda: pytest.fail("model must not load")
    )
    text = f"@{conf} @{tmp_path}/nope.csv @{tmp_path} écris à jean@example.fr"
    _feed_event(monkeypatch, _prompt_event(text, tmp_path))
    assert pii_guard.run_hook() == 0
    assert capsys.readouterr().out == ""


def test_prompt_relative_at_mention_resolves_against_cwd(tmp_path, monkeypatch, capsys):
    """`@clients.csv` is relative to the session's working directory, not the hook's."""
    _csv(tmp_path / "clients.csv", [{"contact": "jean@example.fr"}] * 3)
    _flag_csv(monkeypatch)
    _feed_event(monkeypatch, _prompt_event("regarde @clients.csv", tmp_path))
    assert pii_guard.run_hook() == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "block"


def test_prompt_hook_fails_open(tmp_path, monkeypatch, capsys):
    """An internal error in the prompt path lets the message through, with a stderr line."""
    data = _csv(tmp_path / "clients.csv", [{"contact": "jean@example.fr"}] * 3)

    def boom(files, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(anonymize, "detect_findings", boom)
    _feed_event(monkeypatch, _prompt_event(f"lis @{data}", tmp_path))
    assert pii_guard.run_hook() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "boom" in captured.err


def test_prompt_with_the_bypass_marker_is_never_blocked_nor_scanned(
    tmp_path, monkeypatch, capsys
):
    """`pii:ok` typed by the person lifts the block for the message — and skips the scan, so the
    model is not even loaded."""
    data = _csv(tmp_path / "clients.csv", [{"contact": "jean@example.fr"}] * 3)
    monkeypatch.setattr(
        anonymize,
        "detect_findings",
        lambda files, **kw: pytest.fail("must not scan when the marker is present"),
    )
    _feed_event(monkeypatch, _prompt_event(f"pii:ok lis @{data}", tmp_path))
    assert pii_guard.run_hook() == 0
    assert capsys.readouterr().out == ""


def test_block_reason_announces_the_bypass_marker(tmp_path, monkeypatch, capsys):
    """The person is told how to send the message anyway."""
    data = _csv(tmp_path / "clients.csv", [{"contact": "jean@example.fr"}] * 3)
    _flag_csv(monkeypatch)
    _feed_event(monkeypatch, _prompt_event(f"lis @{data}", tmp_path))
    assert pii_guard.run_hook() == 0
    assert "pii:ok" in json.loads(capsys.readouterr().out)["reason"]


@pytest.mark.parametrize(
    ("name", "values_only"), [("data.anon.xml", True), ("data.xml", False)]
)
def test_only_anonymized_copies_are_scanned_on_values_alone(
    tmp_path, monkeypatch, capsys, name, values_only
):
    """A `.anon.` file keeps its column names, so the name heuristic must not fire on it; any
    other file keeps the full detection."""
    data = tmp_path / name
    data.write_text("<a><b>x</b></a>", encoding="utf-8")
    seen = []

    def spy(files, values_only=False):
        seen.append(values_only)
        return []

    monkeypatch.setattr(anonymize, "detect_findings", spy)
    _feed_event(monkeypatch, _read_event(data))
    assert pii_guard.run_hook() == 0
    assert seen == [values_only]


def test_a_long_text_file_asks_not_fully_analyzed(tmp_path, monkeypatch, capsys):
    """Past the line budget the hook asks with a 'NOT fully analyzed' message even when the lines
    it did read are clean — rather than allowing silently or hanging until the hook timeout."""
    data = tmp_path / "big.log"
    data.write_text(
        "".join(f"[INFO] line {i} ok\n" for i in range(50)), encoding="utf-8"
    )
    monkeypatch.setattr(pii_guard, "MAX_TEXT_LINES", 20)

    def fake(files, values_only=False, text_max_lines=None, truncated=None):
        assert text_max_lines == 20
        if truncated:
            truncated(Path(files[0]).name)
        return []

    monkeypatch.setattr(anonymize, "detect_findings", fake)
    _feed_event(monkeypatch, _read_event(data))
    assert pii_guard.run_hook() == 0
    decision = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
    assert "NOT fully analyzed" in decision["permissionDecisionReason"]
    assert "20" in decision["permissionDecisionReason"]


def _memory_csv(monkeypatch, path: Path) -> Path:
    """Write a PII-bearing CSV and make any scan attempt fail the test.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        path: Where to write the CSV (its parent directory must exist).

    Returns:
        The path of the written CSV.
    """
    data = _csv(path, [{"contact": "jean.dupont@example.fr", "note": "x"}] * 3)
    monkeypatch.setattr(
        anonymize,
        "detect_findings",
        lambda files, **kw: pytest.fail("the agent's own scope must not be scanned"),
    )
    return data


def test_read_of_a_claude_folder_yields_no_decision_and_no_scan(
    tmp_path, monkeypatch, capsys
):
    """A `.claude` folder is the agent's own config/notes: a `Read` of a PII-bearing file
    inside it produces no decision and never reaches the scan."""
    (tmp_path / ".claude").mkdir()
    data = _memory_csv(monkeypatch, tmp_path / ".claude" / "clients.csv")
    _feed_event(monkeypatch, _read_event(data))
    assert pii_guard.run_hook() == 0
    assert capsys.readouterr().out == ""


def test_read_under_the_config_dir_yields_no_decision_and_no_scan(
    tmp_path, monkeypatch, capsys
):
    """The resolved config dir (`CLAUDE_CONFIG_DIR`) is excluded wherever it points — the
    default `projects/<project>/memory` silo is under it mechanically."""
    silo = tmp_path / "silo" / "projects" / "demo" / "memory"
    silo.mkdir(parents=True)
    data = _memory_csv(monkeypatch, silo / "note.csv")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "silo"))
    _feed_event(monkeypatch, _read_event(data))
    assert pii_guard.run_hook() == 0
    assert capsys.readouterr().out == ""


def test_prompt_at_mention_of_the_agent_own_scope_is_never_scanned(
    tmp_path, monkeypatch, capsys
):
    """The same exclusion on the `@file` prompt channel — the criterion is the folder, not
    the access channel."""
    (tmp_path / ".claude").mkdir()
    data = _memory_csv(monkeypatch, tmp_path / ".claude" / "clients.csv")
    _feed_event(monkeypatch, _prompt_event(f"lis @{data}", tmp_path))
    assert pii_guard.run_hook() == 0
    assert capsys.readouterr().out == ""


def test_a_dataset_outside_the_excluded_scope_still_asks(tmp_path, monkeypatch, capsys):
    """Contrast: with `CLAUDE_CONFIG_DIR` pointing at a silo, a PII-bearing file outside it
    still gets the ask decision."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "silo"))
    monkeypatch.setattr(
        anonymize,
        "detect_findings",
        lambda files, **kw: [
            anonymize.Finding("clients.csv", "contact", "detected EMAIL_ADDRESS (3/3)")
        ],
    )
    data = _csv(
        tmp_path / "clients.csv",
        [{"contact": "jean.dupont@example.fr", "note": "x"}] * 3,
    )
    _feed_event(monkeypatch, _read_event(data))
    assert pii_guard.run_hook() == 0
    decision = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
