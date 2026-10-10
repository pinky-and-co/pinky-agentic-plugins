"""Shared status-line layer (`_common.py`) — the surface both entry points import.

Same oracle strategy as its two consumers (tests.md § generator oracle): `render_bar` builds
output, so it gets a whole-string equality oracle, not a fragment check.
"""

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).parent.parent / "plugins" / "pc-statusline" / "scripts"
sys.path.insert(0, str(HOOKS))

import _common as c  # noqa: E402


@pytest.fixture(autouse=True)
def _no_live_process_table(monkeypatch, tmp_path):
    """No real `ps`, no real `HOME`, no shared temp dir for the process-flag cache and the debug
    dump."""
    monkeypatch.setattr(c, "_ps", lambda *_args: "")
    monkeypatch.setattr(c.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_render_bar_oracle():
    # 40% of a 10-cell bar = round(4.0) = 4 filled + 6 empty, filled run wrapped in the passed
    # colour, remainder dimmed — the exact string, separators and resets included.
    assert c.render_bar(40, width=10, color=c.GREEN) == (
        f"{c.GREEN}{'█' * 4}{c.RESET}{c.DIM}{'░' * 6}{c.RESET}"
    )


def test_render_bar_clamps_out_of_range_pct():
    assert c.render_bar(150, width=5, color=c.RED) == c.render_bar(
        100, width=5, color=c.RED
    )
    assert c.render_bar(-20, width=5, color=c.RED) == c.render_bar(
        0, width=5, color=c.RED
    )


def test_render_bar_honours_custom_fill_chars():
    assert c.render_bar(50, width=4, color=c.GREEN, filled="#", empty="-") == (
        f"{c.GREEN}##{c.RESET}{c.DIM}--{c.RESET}"
    )


def test_setup_helpers_never_raise_off_windows():
    # On Mac/Linux both are near no-ops; the contract is only that they never blow up.
    c.enable_ansi_on_windows()
    c.force_utf8_stdout()


# --- the `claude` process's command line --------------------------------------------------------


def test_is_claude_matches_the_binary_not_a_process_citing_it():
    assert c._is_claude("claude --session-id s1")
    assert c._is_claude("/usr/local/bin/claude --session-id s1")
    assert not c._is_claude("vim claude.py")
    assert not c._is_claude("node claude")
    assert not c._is_claude("")


def test_claude_args_by_session_matches_the_claude_process_carrying_the_id(
    monkeypatch,
):
    table = (
        "/bin/zsh -l\n"
        "grep --session-id s1\n"
        "claude --session-id s1 --advisor opus\n"
        "claude --session-id=s2\n"
    )
    monkeypatch.setattr(c, "_ps", lambda *_args: table)
    assert c._claude_args_by_session("s1") == "claude --session-id s1 --advisor opus"
    assert c._claude_args_by_session("s2") == "claude --session-id=s2"
    assert c._claude_args_by_session("s3") is None


def test_claude_args_by_ancestry_walks_up_to_the_claude_process(monkeypatch):
    parents = {"300": "200 -zsh", "200": "100 claude --advisor sonnet"}
    monkeypatch.setattr(c.os, "getppid", lambda: 300)
    monkeypatch.setattr(c, "_ps", lambda *args: parents.get(args[-1], ""))
    assert c._claude_args_by_ancestry() == "claude --advisor sonnet"


def test_claude_args_by_ancestry_stops_at_init_and_on_a_missing_process(monkeypatch):
    monkeypatch.setattr(c.os, "getppid", lambda: 300)
    monkeypatch.setattr(c, "_ps", lambda *args: "1 -zsh" if args[-1] == "300" else "")
    assert c._claude_args_by_ancestry() is None
    monkeypatch.setattr(c, "_ps", lambda *_args: "")
    assert c._claude_args_by_ancestry() is None


@pytest.mark.parametrize(
    "command_line, advisor, debug",
    [
        ("claude --session-id s1 --debug", None, True),
        ("claude --debug --session-id s1", None, True),
        ("claude --session-id s1 --debug=api,hooks", None, True),
        ("claude --session-id s1 --debug api,hooks --advisor opus", "opus", True),
        ("claude --session-id s1 --debug-file /tmp/x.log", None, False),
        ("claude --session-id s1 --debugger", None, False),
        ("claude --session-id s1 --no-debug", None, False),
        ("claude --session-id s1 --advisor=claude-opus-5-5", "claude-opus-5-5", False),
        ("claude --session-id s1", None, False),
    ],
    ids=[
        "bare",
        "first",
        "equals-filter",
        "space-filter",
        "debug-file",
        "prefix",
        "negated",
        "advisor-only",
        "no-flag",
    ],
)
def test_process_flags_reads_whole_token_flags_only(
    monkeypatch, command_line, advisor, debug
):
    monkeypatch.setattr(c, "_ps", lambda *_args: f"{command_line}\n")
    assert c.process_flags("s1") == c.ProcessFlags(advisor, debug)


def test_process_flags_caches_no_flag_as_a_known_result(tmp_path, monkeypatch):
    calls = []

    def counting_ps(*args):
        calls.append(args)
        return "claude --session-id s1\n"

    monkeypatch.setattr(c, "_ps", counting_ps)
    assert c.process_flags("s1") == c.ProcessFlags(None, False)
    assert c.process_flags("s1") == c.ProcessFlags(None, False)
    assert len(calls) == 1
    cache = tmp_path / "statusline-advisor-s1.json"
    assert json.loads(cache.read_text(encoding="utf-8")) == {
        "advisor": None,
        "debug": False,
    }


def test_process_flags_reads_the_cache_shape_that_carried_the_advisor_alone(
    tmp_path, monkeypatch
):
    def _ps_must_not_run(*_args):
        raise AssertionError("ps ran despite a cached reading")

    (tmp_path / "statusline-advisor-s1.json").write_text(
        json.dumps({"advisor": "haiku"}), encoding="utf-8"
    )
    monkeypatch.setattr(c, "_ps", _ps_must_not_run)
    assert c.process_flags("s1") == c.ProcessFlags("haiku", False)


def test_process_flags_retries_after_a_failed_ps(tmp_path, monkeypatch):
    calls = []

    def _failing_ps(*args):
        calls.append(args)
        raise subprocess.SubprocessError("ps failed")

    monkeypatch.setattr(c, "_ps", _failing_ps)
    assert c.process_flags("s1") == c.ProcessFlags(None, False)
    assert c.process_flags("s1") == c.ProcessFlags(None, False)
    assert len(calls) == 2
    assert list(tmp_path.iterdir()) == []


# --- debug dump ---------------------------------------------------------------------------------


def _debug_file(tmp_path, session_id="s1", name="statusline"):
    return (
        tmp_path
        / "home"
        / ".claude"
        / "debug"
        / f"{session_id}__pc-statusline__{name}.json"
    )


@pytest.fixture
def debug_session(monkeypatch):
    monkeypatch.setattr(c, "_ps", lambda *_args: "claude --session-id s1 --debug\n")


def test_dump_debug_payload_moves_a_repeated_payload_to_the_end(
    tmp_path, debug_session
):
    for payload in ({"n": 1}, {"n": 2}, {"n": 1}):
        c.dump_debug_payload(payload, "s1", "statusline")
    stored = json.loads(_debug_file(tmp_path).read_text(encoding="utf-8"))
    assert stored == [{"n": 2}, {"n": 1}]


def test_dump_debug_payload_replaces_a_stored_file_that_is_not_a_payload_list(
    tmp_path, debug_session
):
    path = _debug_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not a list", encoding="utf-8")
    c.dump_debug_payload({"n": 1}, "s1", "statusline")
    assert json.loads(path.read_text(encoding="utf-8")) == [{"n": 1}]


@pytest.mark.parametrize("session_id", ["../escape", "a/b", "?", "", None, 7])
def test_dump_debug_payload_ignores_a_session_id_unfit_for_a_file_name(
    tmp_path, debug_session, session_id
):
    c.dump_debug_payload({"n": 1}, session_id, "statusline")
    assert not (tmp_path / "home").exists()


def test_write_json_atomic_leaves_no_temp_file_and_raises_on_a_missing_dir(tmp_path):
    target = tmp_path / "out.json"
    c.write_json_atomic(str(target), {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["out.json"]
    with pytest.raises(OSError):
        c.write_json_atomic(str(tmp_path / "missing" / "out.json"), {"a": 1})


def test_write_json_atomic_creates_the_file_owner_only(tmp_path):
    target = tmp_path / "out.json"
    c.write_json_atomic(str(target), {"a": 1})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_json_atomic_leaves_no_temp_file_when_the_payload_is_not_serialisable(
    tmp_path, debug_session
):
    target = tmp_path / "out.json"
    with pytest.raises(TypeError):
        c.write_json_atomic(str(target), {"a": object()})
    assert list(tmp_path.iterdir()) == []

    c.dump_debug_payload({"a": object()}, "s1", "statusline")
    debug_dir = tmp_path / "home" / ".claude" / "debug"
    assert list(debug_dir.iterdir()) == []
