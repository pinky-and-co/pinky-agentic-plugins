#!/usr/bin/env python3
"""Tests for the pc-rtk plugin — run with: python3 -m pytest tests/test_pc_rtk.py"""

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "pc-rtk"
GAIN = PLUGIN / "skills" / "gain" / "scripts" / "gain.sh"
PAYLOAD = json.dumps(
    {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git log -n 3"},
    }
)
REWRITE_OUTPUT = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecisionReason": "RTK auto-rewrite",
        "updatedInput": {"command": "rtk git log -n 3"},
    }
}


def _hooks() -> dict:
    return json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]


def _hook_command() -> str:
    (entry,) = _hooks()["PreToolUse"]
    return entry["hooks"][0]["command"]


def _stub_rtk(directory: Path, body: str) -> Path:
    """Write an executable `rtk` shell stub into `directory` and return the directory."""
    directory.mkdir(exist_ok=True)
    stub = directory / "rtk"
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return directory


def _run_hook(path: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/sh", "-c", _hook_command()],
        input=PAYLOAD,
        capture_output=True,
        text=True,
        env={"PATH": path},
    )


def _keys(node) -> set[str]:
    """Every dict key at any depth."""
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md has no frontmatter"
    return dict(
        line.split(":", 1) for line in match.group(1).splitlines() if ":" in line
    )


@pytest.fixture
def readme() -> str:
    return (PLUGIN / "README.md").read_text()


def test_hooks_json_declares_exactly_the_two_events():
    hooks = _hooks()
    assert sorted(hooks) == ["PreToolUse", "UserPromptExpansion"]
    for entries in hooks.values():
        (entry,) = entries
        assert len(entry["hooks"]) == 1
    (pre,) = hooks["PreToolUse"]
    assert pre["matcher"] == "Bash"
    assert "rtk hook claude" in _hook_command()


def test_hook_fails_open_when_rtk_is_absent(tmp_path):
    proc = _run_hook(str(tmp_path))
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_hook_passes_stdin_to_rtk(tmp_path):
    path = _stub_rtk(tmp_path / "bin", "cat")
    proc = _run_hook(f"{path}:/usr/bin:/bin")
    assert proc.returncode == 0
    assert proc.stdout == PAYLOAD


def test_hook_never_blocks_when_rtk_errors(tmp_path):
    path = _stub_rtk(tmp_path / "bin", "exit 2")
    proc = _run_hook(f"{path}:/usr/bin:/bin")
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_hook_relays_the_rewrite_without_a_permission_decision(tmp_path):
    path = _stub_rtk(
        tmp_path / "bin", f"cat <<'EOF'\n{json.dumps(REWRITE_OUTPUT)}\nEOF"
    )
    proc = _run_hook(f"{path}:/usr/bin:/bin")
    assert proc.returncode == 0
    output = json.loads(proc.stdout)
    assert output == REWRITE_OUTPUT
    assert "permissionDecision" not in _keys(output["hookSpecificOutput"])


@pytest.mark.skipif(shutil.which("rtk") is None, reason="rtk is not installed")
def test_real_rtk_rewrites_a_rewritable_command():
    proc = _run_hook(os.environ["PATH"])
    assert proc.returncode == 0
    output = json.loads(proc.stdout)
    assert output["hookSpecificOutput"]["updatedInput"]["command"] == (
        "rtk git log -n 3"
    )
    # Contract chosen: the installed rtk binary now emits permissionDecision
    # "allow" next to updatedInput (measured against the real binary), so the
    # test asserts what it emits rather than pinning the binary to the old one.
    assert output["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert (
        output["hookSpecificOutput"]["permissionDecisionReason"] == "RTK auto-rewrite"
    )


def test_gain_skill_pre_approves_exactly_the_script_and_the_bare_rtk_gain():
    text = (PLUGIN / "skills" / "gain" / "SKILL.md").read_text()
    allowed = {e.strip() for e in _frontmatter(text)["allowed-tools"].split(",")}
    assert allowed == {
        "Bash(${CLAUDE_PLUGIN_ROOT}/skills/gain/scripts/gain.sh:*)",
        "Bash(rtk gain)",
    }


def test_gain_script_is_executable():
    assert os.stat(GAIN).st_mode & 0o111


def _run_gain(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, Path]:
    calls = tmp_path / "calls"
    path = _stub_rtk(tmp_path / "bin", f'printf "%s\\n" "$@" >> "{calls}"')
    proc = subprocess.run(
        ["/bin/bash", str(GAIN), *args],
        capture_output=True,
        text=True,
        env={"PATH": f"{path}:/usr/bin:/bin"},
    )
    return proc, calls


def test_gain_script_reports_a_missing_rtk(tmp_path):
    proc = subprocess.run(
        ["/bin/bash", str(GAIN)],
        capture_output=True,
        text=True,
        env={"PATH": str(tmp_path)},
    )
    assert proc.returncode == 1
    assert "brew install rtk" in proc.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["--reset"],
        ["--reset", "--yes"],
        ["--yes"],
        ["--daily", "--reset"],
        ["x"],
        ["--tier", "--reset"],
        ["--tier=--reset"],
        ["-f", "-x"],
        ["--"],
        ["-t"],
    ],
)
def test_gain_script_refuses_anything_but_read_options(tmp_path, args):
    proc, calls = _run_gain(tmp_path, *args)
    assert proc.returncode == 1
    assert "refused" in proc.stdout
    assert not calls.exists()


@pytest.mark.parametrize(
    "args",
    [[], ["--daily"], ["-t", "pro --reset"], ["--format=json"], ["-p", "-H"]],
)
def test_gain_script_passes_read_options_through(tmp_path, args):
    proc, calls = _run_gain(tmp_path, *args)
    assert proc.returncode == 0
    assert calls.read_text().splitlines() == ["gain", *args]


def _direct_entry() -> dict:
    (entry,) = _hooks()["UserPromptExpansion"]
    return entry


def _fire_direct(
    tmp_path: Path, command_args: str
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the declared UserPromptExpansion command with a recording stub `rtk` first on PATH."""
    calls = tmp_path / "calls"
    stub = _stub_rtk(
        tmp_path / "bin",
        f'echo "stub gain output"\nprintf "%s\\n" "$@" >> "{calls}"',
    )
    command = _direct_entry()["hooks"][0]["command"].replace(
        "${CLAUDE_PLUGIN_ROOT}", str(PLUGIN)
    )
    python_dir = Path(shutil.which("python3") or "/usr/bin/python3").parent
    proc = subprocess.run(
        ["/bin/sh", "-c", command],
        input=json.dumps({"command_args": command_args, "cwd": str(tmp_path)}),
        capture_output=True,
        text=True,
        env={"PATH": f"{stub}:{python_dir}:/usr/bin:/bin"},
    )
    return proc, calls


def test_direct_entry_matches_only_the_gain_command():
    matcher = _direct_entry()["matcher"]
    assert re.fullmatch(matcher, "pc-rtk:gain")
    assert not re.fullmatch(matcher, "pc-rtk:gains")
    assert not re.fullmatch(matcher, "pc-git:gain")


def test_direct_entry_timeouts_nest():
    (hook,) = _direct_entry()["hooks"]
    runner_timeout = re.search(r"--timeout (\d+)", hook["command"])
    assert runner_timeout
    assert int(runner_timeout.group(1)) < int(hook["timeout"])


def test_direct_bare_gain_blocks_with_the_script_output(tmp_path):
    proc, calls = _fire_direct(tmp_path, "")
    assert proc.returncode == 0
    output = json.loads(proc.stdout)
    assert output["decision"] == "block"
    assert output["reason"] == "stub gain output"
    assert calls.read_text().splitlines() == ["gain"]


def test_direct_gain_with_a_read_flag_reaches_rtk(tmp_path):
    proc, calls = _fire_direct(tmp_path, "--daily")
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["decision"] == "block"
    assert calls.read_text().splitlines() == ["gain", "--daily"]


@pytest.mark.parametrize("args", ["--reset", "--yes", "--daily --reset", "-t pro"])
def test_direct_gain_falls_back_silently_otherwise(tmp_path, args):
    proc, calls = _fire_direct(tmp_path, args)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not calls.exists()


def _grammar() -> str:
    command = _direct_entry()["hooks"][0]["command"]
    match = re.search(r"--grammar '([^']*)'", command)
    assert match, "no --grammar in the direct command"
    return match.group(1).replace('\\"', '"')


def _run_runner_with_recorder(
    tmp_path: Path, command_args: str
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the vendored runner with the real grammar over a script that records its argv."""
    calls = tmp_path / "calls"
    script = tmp_path / "record.sh"
    script.write_text(
        f'#!/bin/bash\n: >> "{calls}"\n'
        f'[ "$#" -eq 0 ] || printf \'%s\\n\' "$@" >> "{calls}"\n'
        "echo ran\n"
    )
    proc = subprocess.run(
        [
            "python3",
            str(PLUGIN / "hooks" / "pc_rtk_direct_command_runner.py"),
            "--script",
            str(script),
            "--interpreter",
            "bash",
            "--timeout",
            "30",
            "--grammar",
            _grammar(),
        ],
        input=json.dumps({"command_args": command_args, "cwd": str(tmp_path)}),
        capture_output=True,
        text=True,
    )
    return proc, calls


@pytest.mark.parametrize(
    "args",
    [
        "--reset",
        "--yes",
        "--daily --reset",
        "--tier=pro",
        "--format=json",
        "-pg",
        "-t pro",
        "--daily 'x y'",
    ],
)
def test_grammar_never_runs_the_script_for_anything_but_plain_read_flags(
    tmp_path, args
):
    proc, calls = _run_runner_with_recorder(tmp_path, args)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not calls.exists()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("", []),
        ("--daily", ["--daily"]),
        ("--daily --all", ["--daily", "--all"]),
        ("-p -H", ["-p", "-H"]),
        ('"--daily"', ["--daily"]),
    ],
)
def test_grammar_hands_plain_read_flags_to_the_script_token_by_token(
    tmp_path, args, expected
):
    proc, calls = _run_runner_with_recorder(tmp_path, args)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["decision"] == "block"
    assert calls.read_text().splitlines() == expected
    assert json.loads(proc.stdout)["reason"] == "ran"


def test_vendored_runner_is_identical_to_the_pc_git_copy():
    rtk = PLUGIN / "hooks" / "pc_rtk_direct_command_runner.py"
    git = PLUGIN.parent / "pc-git" / "hooks" / "pc_git_direct_command_runner.py"
    assert rtk.read_bytes() == git.read_bytes()


def test_readme_documents_the_pi_extension_and_teardown(readme):
    for required in ("rtk init -g --agent pi", "--uninstall", "brew uninstall rtk"):
        assert required in readme


def test_readme_never_mentions_trust_or_telemetry_opt_in(readme):
    assert "rtk trust" not in readme
    assert "trust-filters" not in readme
    assert "telemetry" not in readme.lower()


def test_readme_init_g_only_appears_with_the_pi_agent(readme):
    for match in re.finditer(r"rtk init -g", readme):
        assert readme.startswith(" --agent pi", match.end())
