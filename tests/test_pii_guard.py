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


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch) -> None:
    """Keep the verdict store out of the real ~/.cache, and one test's verdicts out of the next."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


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

    def spy(files, values_only=False, fmt=None):
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

    def fake(files, values_only=False, text_max_lines=None, truncated=None, fmt=None):
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


# ── helpers for the band mod (`hooks/pii-hold`) ───────────────────────────────


def _one_finding(monkeypatch) -> None:
    monkeypatch.setattr(
        anonymize,
        "detect_findings",
        lambda files, **kw: [
            anonymize.Finding("clients.csv", "contact", "detected EMAIL_ADDRESS (3/3)")
        ],
    )


def test_verdict_names_the_columns_and_offers_the_copy(tmp_path, monkeypatch):
    data = _csv(tmp_path / "clients.csv", [{"contact": "a@example.fr", "note": "x"}])
    _one_finding(monkeypatch)
    assert pii_guard.verdict(_read_event(data), anonymize) == {
        "flagged": True,
        "kind": "pii",
        "path": str(data),
        "columns": ["contact"],
        "anonymize": True,
    }


def test_verdict_of_a_clean_file_or_another_tool_is_not_flagged(tmp_path, monkeypatch):
    data = _csv(tmp_path / "c.csv", [{"contact": "a", "note": "x"}])
    monkeypatch.setattr(anonymize, "detect_findings", lambda files, **kw: [])
    assert pii_guard.verdict(_read_event(data), anonymize) == {"flagged": False}
    assert pii_guard.verdict({"tool_name": "Bash"}, anonymize) == {"flagged": False}


def test_verdict_of_an_oversize_file_offers_no_copy(tmp_path, monkeypatch):
    data = _csv(tmp_path / "big.csv", [{"contact": "a@example.fr", "note": "x"}])
    monkeypatch.setattr(pii_guard, "MAX_BYTES", 1)
    assert pii_guard.verdict(_read_event(data), anonymize) == {
        "flagged": True,
        "kind": "oversize",
        "path": str(data),
        "columns": [],
        "anonymize": False,
    }


def test_verdict_of_an_anonymized_copy_offers_no_second_copy(tmp_path, monkeypatch):
    data = _csv(
        tmp_path / "clients.anon.csv", [{"contact": "a@example.fr", "note": "x"}]
    )
    _one_finding(monkeypatch)
    assert pii_guard.verdict(_read_event(data), anonymize)["anonymize"] is False


def test_make_copy_masks_the_flagged_columns_and_leaves_the_original(
    tmp_path, monkeypatch
):
    data = _csv(tmp_path / "clients.csv", [{"contact": "a@example.fr", "note": "x"}])
    original = data.read_text(encoding="utf-8")
    _one_finding(monkeypatch)
    copy = pii_guard.make_copy(data, anonymize)
    assert copy == str(tmp_path / "clients.anon.csv")
    assert data.read_text(encoding="utf-8") == original
    rows = list(csv.DictReader(Path(copy).open(encoding="utf-8")))
    assert rows[0]["note"] == "x"
    assert rows[0]["contact"].startswith("CONTACT_")


def test_make_copy_without_a_finding_writes_nothing(tmp_path, monkeypatch):
    data = _csv(tmp_path / "c.csv", [{"contact": "a", "note": "x"}])
    monkeypatch.setattr(anonymize, "detect_findings", lambda files, **kw: [])
    assert pii_guard.make_copy(data, anonymize) is None
    assert list(tmp_path.glob("*.anon.*")) == []


def test_main_verdict_mode_prints_json(tmp_path, monkeypatch, capsys):
    data = _csv(tmp_path / "clients.csv", [{"contact": "a@example.fr", "note": "x"}])
    _one_finding(monkeypatch)
    _feed_event(monkeypatch, _read_event(data))
    assert pii_guard.main(["--verdict"]) == 0
    assert json.loads(capsys.readouterr().out)["columns"] == ["contact"]


def test_main_anonymize_mode_prints_the_copy_path(tmp_path, monkeypatch, capsys):
    data = _csv(tmp_path / "clients.csv", [{"contact": "a@example.fr", "note": "x"}])
    _one_finding(monkeypatch)
    assert pii_guard.main(["--anonymize", str(data)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "copy": str(tmp_path / "clients.anon.csv")
    }


def test_main_modes_exit_one_when_nothing_can_be_answered(
    tmp_path, monkeypatch, capsys
):
    data = _csv(tmp_path / "c.csv", [{"contact": "a", "note": "x"}])
    monkeypatch.setattr(anonymize, "detect_findings", lambda files, **kw: [])
    assert pii_guard.main(["--anonymize", str(data)]) == 1
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert pii_guard.main(["--verdict"]) == 1


# ── the verdict store: one scan per version of a content and of the guard's code ──


@pytest.fixture
def store(tmp_path, monkeypatch) -> Path:
    """The cache folder, pointed at a tmp dir so the real ~/.cache is never written."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path / "cache" / "pc-privacy" / "verdicts.json"


def _counting_scan(monkeypatch) -> list[list[str]]:
    """Make every analysis find one sensitive column, and record which files were analyzed."""
    scans: list[list[str]] = []

    def fake(files, **kw):
        scans.append(files)
        return [anonymize.Finding("f", "contact", "detected EMAIL_ADDRESS (3/3)")]

    monkeypatch.setattr(anonymize, "detect_findings", fake)
    return scans


def test_a_content_already_judged_is_not_scanned_again(tmp_path, monkeypatch, store):
    """Two verdicts on one content run the analyzer once, whichever name or place the file has."""
    scans = _counting_scan(monkeypatch)
    first = _csv(tmp_path / "clients.csv", [{"contact": "a@example.fr"}])
    copy = tmp_path / "elsewhere.csv"
    copy.write_bytes(first.read_bytes())
    one = pii_guard.verdict(_read_event(first), anonymize)
    again = pii_guard.verdict(_read_event(first), anonymize)
    renamed = pii_guard.verdict(_read_event(copy), anonymize)
    assert len(scans) == 1
    assert one == again
    assert renamed == {**one, "path": str(copy)}


def test_the_hook_reuses_a_verdict_the_band_asked_for_first(
    tmp_path, monkeypatch, store, capsys
):
    scans = _counting_scan(monkeypatch)
    data = _csv(tmp_path / "clients.csv", [{"contact": "a@example.fr"}])
    pii_guard.verdict(_read_event(data), anonymize)
    _feed_event(monkeypatch, _read_event(data))
    assert pii_guard.run_hook() == 0
    decision = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
    assert "contact" in decision["permissionDecisionReason"]
    assert len(scans) == 1


def test_a_clean_verdict_is_stored_too(tmp_path, monkeypatch, store):
    scans: list[list[str]] = []
    monkeypatch.setattr(
        anonymize, "detect_findings", lambda files, **kw: scans.append(files) or []
    )
    data = _csv(tmp_path / "c.csv", [{"contact": "a", "note": "x"}])
    for _ in range(2):
        assert pii_guard.verdict(_read_event(data), anonymize) == {"flagged": False}
    assert len(scans) == 1


def _key_and_scans(tmp_path, monkeypatch) -> tuple[str, int]:
    """Key and cumulative scan count after one verdict on a fixed file."""
    data = tmp_path / "fixed.csv"
    if not data.exists():
        _csv(data, [{"contact": "a@example.fr"}])
    scans = _scan_log(monkeypatch)
    pii_guard.verdict(_read_event(data), anonymize)
    return pii_guard._cache_key(data, anonymize), len(scans)


def _scan_log(monkeypatch) -> list[list[str]]:
    """Reuse one scan recorder across calls so counts accumulate within a test."""
    existing = getattr(anonymize.detect_findings, "scans", None)
    if existing is not None:
        return existing
    scans = _counting_scan(monkeypatch)
    anonymize.detect_findings.scans = scans  # type: ignore[attr-defined]
    return scans


def test_a_changed_decision_gate_source_changes_the_key_and_rescans(
    tmp_path, monkeypatch, store
):
    gate = tmp_path / "decision_gate.py"
    gate.write_text("# rule v1\n")
    monkeypatch.setattr(pii_guard, "GATE_SOURCE", gate)
    key, scans = _key_and_scans(tmp_path, monkeypatch)
    gate.write_text("# rule v2\n")
    new_key, new_scans = _key_and_scans(tmp_path, monkeypatch)
    assert new_key != key
    assert (scans, new_scans) == (1, 2)


def test_a_changed_decision_model_changes_the_key_and_rescans(
    tmp_path, monkeypatch, store
):
    monkeypatch.delenv("ONE_DECISION_MODEL", raising=False)
    key, scans = _key_and_scans(tmp_path, monkeypatch)
    monkeypatch.setenv("ONE_DECISION_MODEL", "other-model")
    new_key, new_scans = _key_and_scans(tmp_path, monkeypatch)
    assert new_key != key
    assert (scans, new_scans) == (1, 2)


def test_a_changed_spacy_model_version_changes_the_key_and_rescans(
    tmp_path, monkeypatch, store
):
    versions = {anonymize.SPACY_MODEL: "3.7.0"}

    def version(name: str) -> str:
        if name not in versions:
            raise pii_guard.importlib.metadata.PackageNotFoundError(name)
        return versions[name]

    monkeypatch.setattr(pii_guard.importlib.metadata, "version", version)
    key, scans = _key_and_scans(tmp_path, monkeypatch)
    versions[anonymize.SPACY_MODEL] = "3.8.0"
    new_key, new_scans = _key_and_scans(tmp_path, monkeypatch)
    assert new_key != key
    assert (scans, new_scans) == (1, 2)
    del versions[anonymize.SPACY_MODEL]
    missing_key, _ = _key_and_scans(tmp_path, monkeypatch)
    assert missing_key not in (key, new_key)


def test_a_changed_content_is_scanned_again(tmp_path, monkeypatch, store):
    scans = _counting_scan(monkeypatch)
    data = _csv(tmp_path / "clients.csv", [{"contact": "a@example.fr"}])
    pii_guard.verdict(_read_event(data), anonymize)
    _csv(data, [{"contact": "b@example.fr"}])
    pii_guard.verdict(_read_event(data), anonymize)
    assert len(scans) == 2


def test_a_changed_guard_code_scans_everything_again(tmp_path, monkeypatch, store):
    scans = _counting_scan(monkeypatch)
    data = _csv(tmp_path / "clients.csv", [{"contact": "a@example.fr"}])
    pii_guard.verdict(_read_event(data), anonymize)
    edited = tmp_path / "pii_guard_edited.py"
    edited.write_text(Path(pii_guard.__file__).read_text() + "\n# a new rule\n")
    monkeypatch.setattr(pii_guard, "__file__", str(edited))
    pii_guard.verdict(_read_event(data), anonymize)
    assert len(scans) == 2


def test_an_unwritable_cache_folder_still_returns_the_verdict_and_scans(
    tmp_path, monkeypatch
):
    blocker = tmp_path / "cache"
    blocker.write_text("a file where the folder should go", encoding="utf-8")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))
    scans = _counting_scan(monkeypatch)
    data = _csv(tmp_path / "clients.csv", [{"contact": "a@example.fr"}])
    first = pii_guard.verdict(_read_event(data), anonymize)
    second = pii_guard.verdict(_read_event(data), anonymize)
    assert first == second
    assert first["flagged"] is True
    assert len(scans) == 2


def test_without_xdg_cache_home_the_store_is_under_the_home_cache(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert pii_guard._store() == tmp_path / ".cache" / "pc-privacy" / "verdicts.json"


def test_the_store_keeps_the_500_most_recent_verdicts(tmp_path, monkeypatch, store):
    _counting_scan(monkeypatch)
    keys = []
    for i in range(501):
        data = _csv(tmp_path / f"c{i}.csv", [{"contact": f"{i}@example.fr"}])
        keys.append(pii_guard._cache_key(data, anonymize))
        pii_guard.verdict(_read_event(data), anonymize)
    stored = [e["key"] for e in json.loads(store.read_text(encoding="utf-8"))]
    assert stored == keys[1:]


def test_the_store_holds_hashes_verdict_and_column_names_only(
    tmp_path, monkeypatch, store
):
    _counting_scan(monkeypatch)
    secret = "jean.dupont@example.fr"  # pragma: allowlist secret
    data = _csv(tmp_path / "clients.csv", [{"contact": secret}])
    pii_guard.verdict(_read_event(data), anonymize)
    raw = store.read_text(encoding="utf-8")
    assert secret not in raw
    (entry,) = json.loads(raw)
    assert entry == {
        "key": pii_guard._cache_key(data, anonymize),
        "flagged": True,
        "kind": "pii",
        "columns": ["contact"],
    }


def test_a_guarded_read_decides_the_format_once(tmp_path, monkeypatch, store):
    """`_scan` and `detect_findings` used to each run `file` on the same target."""
    seen: list[Path] = []
    real = anonymize.detect_format

    def counting(path):
        seen.append(path)
        return real(path)

    monkeypatch.setattr(anonymize, "detect_format", counting)
    monkeypatch.setattr(anonymize, "_build_analyzer", lambda: None)
    monkeypatch.setattr(anonymize, "_column_samples", lambda path, fmt: {})
    data = _csv(
        tmp_path / "clients.csv", [{"contact": "a@example.fr", "note": "x"}] * 5
    )
    pii_guard.verdict(_read_event(data), anonymize)
    assert len(seen) == 1


@requires_model
def test_a_markdown_converted_by_pc_read_is_scanned_as_prose(tmp_path):
    """`.md` is a code format, except in the folder `pc-read` writes a conversion into."""
    body = "".join(
        f"Client {i} : jean.dupont{i}@example.fr, IBAN FR76 3000 6000 0112 3456 7890 189\n"
        for i in range(6)
    )
    converted = tmp_path / "pc-read-Ab12" / "report.md"
    converted.parent.mkdir()
    converted.write_text(body, encoding="utf-8")
    elsewhere = tmp_path / "notes" / "report.md"
    elsewhere.parent.mkdir()
    elsewhere.write_text(body, encoding="utf-8")
    assert pii_guard.verdict(_read_event(converted), anonymize)["flagged"] is True
    assert pii_guard.verdict(_read_event(elsewhere), anonymize) == {"flagged": False}


@pytest.mark.parametrize(
    "bad",
    [
        {"flagged": "true", "kind": "pii", "columns": ["contact"]},
        {"flagged": True, "kind": 123, "columns": ["contact"]},
        {"flagged": True, "kind": "pii", "columns": "x"},
    ],
    ids=["flagged-as-string", "kind-as-int", "columns-as-string"],
)
def test_a_malformed_stored_entry_is_a_cache_miss_not_a_verdict(
    tmp_path, monkeypatch, store, bad
):
    """A hand-edited or corrupt entry under the right key is rescanned, never trusted as CLEAN."""
    scans = _counting_scan(monkeypatch)
    data = _csv(tmp_path / "clients.csv", [{"contact": "a@example.fr"}])
    key = pii_guard._cache_key(data, anonymize)
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps([{"key": key, **bad}]), encoding="utf-8")
    result = pii_guard.verdict(_read_event(data), anonymize)
    assert result["flagged"] is True
    assert result["columns"] == ["contact"]
    assert len(scans) == 1


def test_a_file_rewritten_during_the_scan_does_not_store_its_verdict_under_the_old_content(
    tmp_path, monkeypatch, store
):
    """A clean verdict for a content that changed mid-scan must not be served to a PII file."""
    scans = _counting_scan(monkeypatch)
    pii_rows = [{"contact": "a@example.fr"}]
    racing = _csv(tmp_path / "racing.csv", pii_rows)
    twin = tmp_path / "twin.csv"
    twin.write_bytes(racing.read_bytes())
    real_analyze = pii_guard._analyze
    calls: list[Path] = []

    def swapping(path, fmt, anon):
        calls.append(path)
        if len(calls) == 1:
            _csv(path, [{"note": "nothing here"}])
            return None
        return real_analyze(path, fmt, anon)

    monkeypatch.setattr(pii_guard, "_analyze", swapping)
    assert pii_guard._judge(racing, anonymize) is None
    judged = pii_guard._judge(twin, anonymize)
    assert judged is not None
    assert judged[0] == "pii"
    assert calls == [racing, twin]
    assert len(scans) == 1
