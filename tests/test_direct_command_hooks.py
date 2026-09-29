#!/usr/bin/env python3
"""Tests for the `UserPromptExpansion` registries of pc-git and pc-kanban — run with: python3 -m pytest tests/test_direct_command_hooks.py"""

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).parent.parent / "plugins"
PLUGIN_NAMES = ("pc-git", "pc-kanban")


def _entries():
    """Yield (plugin, matcher, argv-template) for every registered direct command."""
    for plugin in PLUGIN_NAMES:
        hooks = json.loads((PLUGINS / plugin / "hooks" / "hooks.json").read_text())
        for entry in hooks["hooks"]["UserPromptExpansion"]:
            yield plugin, entry["matcher"], entry["hooks"][0]["command"]


def _argv(plugin, command):
    root = str(PLUGINS / plugin)
    argv = shlex.split(command.replace("${CLAUDE_PLUGIN_ROOT}", root))
    argv[0] = sys.executable
    return argv


def _fire(plugin, command, command_args, tmp_path):
    proc = subprocess.run(
        _argv(plugin, command),
        input=json.dumps({"command_args": command_args, "cwd": str(tmp_path)}),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "CLAUDE_PROJECT_DIR": str(tmp_path)},
    )
    assert proc.returncode == 0
    return proc.stdout


def test_registry_lists_the_three_direct_commands():
    matchers = sorted(m for _, m, _ in _entries())
    assert matchers == [
        "^pc-git:clean-gone$",
        "^pc-kanban:init$",
        "^pc-kanban:permission-change$",
    ]


@pytest.mark.parametrize("plugin,matcher,command", list(_entries()))
def test_entry_points_at_existing_runner_and_script(plugin, matcher, command):
    argv = _argv(plugin, command)
    assert Path(argv[1]).is_file()
    assert Path(argv[argv.index("--script") + 1]).is_file()
    grammar = json.loads(argv[argv.index("--grammar") + 1])
    assert grammar["forms"]


@pytest.mark.parametrize("plugin,matcher,command", list(_entries()))
def test_entry_falls_back_silently_on_injection_and_flags(
    plugin, matcher, command, tmp_path
):
    for bad in ("$(id)", "; rm -rf x", "`id`", "a\nb", "--root /x", "freeze --root /x"):
        assert _fire(plugin, command, bad, tmp_path) == "", bad


def test_permission_change_status_is_blocked_with_the_script_output(tmp_path):
    (entry,) = [e for e in _entries() if e[1].endswith("permission-change$")]
    out = json.loads(_fire(entry[0], entry[2], "status", tmp_path))
    assert out["decision"] == "block"
    assert out["reason"].startswith("careful:")


def test_permission_change_rejects_bare_freeze(tmp_path):
    (entry,) = [e for e in _entries() if e[1].endswith("permission-change$")]
    assert _fire(entry[0], entry[2], "freeze", tmp_path) == ""


def test_permission_change_accepts_a_real_path_but_not_a_flag_or_bare_dot_dot_flag(
    tmp_path,
):
    (entry,) = [e for e in _entries() if e[1].endswith("permission-change$")]
    out = json.loads(_fire(entry[0], entry[2], "deny /tmp/some-dir", tmp_path))
    assert out["decision"] == "block"
    assert "deny:" in out["reason"]
    assert _fire(entry[0], entry[2], "deny -x", tmp_path) == ""
