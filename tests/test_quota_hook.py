"""pc-statusline quota hook (`hooks/quota_refresh.py` + `hooks/hooks.json`) — tests.

The invariants that matter: the hook never fails a session (exit 0), never prints on stdout (the
harness injects a `SessionStart` / `UserPromptSubmit` hook's stdout into the model's context), and
the four events it is registered on each call it with a mode it knows.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "pc-statusline"
sys.path.insert(0, str(PLUGIN / "scripts"))

import _quota  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "quota_refresh", PLUGIN / "hooks" / "quota_refresh.py"
)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


# --- the registration contract ------------------------------------------------------------------


def _registered_commands():
    config = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    return {
        event: [h for group in groups for h in group["hooks"]]
        for event, groups in config["hooks"].items()
    }


def test_hooks_json_registers_the_four_events():
    assert set(_registered_commands()) == {
        "SessionStart",
        "UserPromptSubmit",
        "PostToolUse",
        "Stop",
    }


def test_hooks_json_commands_call_the_hook_with_a_known_mode_exactly_once_each():
    modes = []
    for event, hooks in _registered_commands().items():
        assert len(hooks) == 1, f"{event}: expected a single command"
        command = hooks[0]["command"]
        assert command.startswith(
            'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/quota_refresh.py" '
        )
        modes.append(command.rsplit(" ", 1)[1])
    assert sorted(modes) == sorted(hook.MIN_AGE_S)


def test_hooks_json_every_command_is_async_so_no_prompt_ever_waits_on_the_network():
    for event, hooks in _registered_commands().items():
        assert hooks[0]["type"] == "command"
        assert hooks[0]["async"] is True, event
        assert hooks[0]["timeout"] >= 10, event


def test_every_registered_command_points_at_a_script_that_exists():
    """The command runs through `python3`, so no executable bit is needed (and none survives a
    copy that drops file modes) — but the path it names must resolve inside the plugin."""
    for event, hooks in _registered_commands().items():
        quoted_path = hooks[0]["command"].split('"')[1]
        script = Path(quoted_path.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN)))
        assert script.is_file(), f"{event}: {quoted_path} does not exist"


# --- MIN_AGE_S — what each event tolerates ------------------------------------------------------


def test_a_session_start_always_forces_a_fetch():
    assert hook.MIN_AGE_S["session"] == 0


def test_the_tool_edge_is_throttled_harder_than_the_prompt_and_turn_edges():
    """PostToolUse fires after every tool call — unthrottled it would be polling."""
    assert hook.MIN_AGE_S["tool"] > hook.MIN_AGE_S["prompt"] > 0
    assert hook.MIN_AGE_S["tool"] > hook.MIN_AGE_S["stop"] > 0


# --- main() -------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["session", "prompt", "stop", "tool"])
def test_main_passes_each_modes_throttle_to_refresh(monkeypatch, mode):
    seen = []
    monkeypatch.setattr(
        _quota, "refresh", lambda min_age_s: seen.append(min_age_s) or "updated"
    )
    assert hook.main(["quota_refresh.py", mode]) == 0
    assert seen == [hook.MIN_AGE_S[mode]]


@pytest.mark.parametrize("argv", [["quota_refresh.py"], ["quota_refresh.py", "bogus"]])
def test_main_ignores_a_missing_or_unknown_mode(monkeypatch, argv):
    calls = []
    monkeypatch.setattr(_quota, "refresh", lambda min_age_s: calls.append(min_age_s))
    assert hook.main(argv) == 0
    assert calls == []


@pytest.mark.parametrize("status", ["updated", "fresh", "no-provider"])
def test_main_is_silent_on_the_uneventful_outcomes(monkeypatch, capsys, status):
    monkeypatch.setattr(_quota, "refresh", lambda min_age_s: status)
    assert hook.main(["quota_refresh.py", "prompt"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


@pytest.mark.parametrize("status", ["failed", "no-token"])
def test_main_reports_a_real_problem_on_stderr_only(monkeypatch, capsys, status):
    monkeypatch.setattr(_quota, "refresh", lambda min_age_s: status)
    assert hook.main(["quota_refresh.py", "prompt"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert status in captured.err


def test_main_never_fails_the_session_when_refresh_raises(monkeypatch, capsys):
    def _boom(min_age_s):
        raise RuntimeError("boom")

    monkeypatch.setattr(_quota, "refresh", _boom)
    assert hook.main(["quota_refresh.py", "session"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "boom" in captured.err


# --- the real entry point (subprocess, real stdin/stdout) ---------------------------------------


def _run_hook(mode, base_url, tmp_path):
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    env.pop(
        "ANTHROPIC_AUTH_TOKEN", None
    )  # never let a real token reach a network call in a test
    env.pop("ANTHROPIC_BASE_URL", None)
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    return subprocess.run(
        [sys.executable, str(PLUGIN / "hooks" / "quota_refresh.py"), mode],
        input="{}",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        env=env,
        check=False,
    )


@pytest.mark.parametrize("mode", ["session", "prompt", "stop", "tool"])
def test_entry_point_on_anthropic_is_a_silent_no_op(tmp_path, mode):
    result = _run_hook(mode, "https://api.anthropic.com", tmp_path)
    assert result.returncode == 0
    assert result.stdout == "" and result.stderr == ""
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("mode", ["session", "prompt", "stop", "tool"])
def test_entry_point_under_a_provider_without_a_token_exits_0_and_writes_nothing(
    tmp_path, mode
):
    result = _run_hook(mode, "https://api.z.ai/api/anthropic", tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""
    assert "no-token" in result.stderr
    assert list(tmp_path.iterdir()) == []
