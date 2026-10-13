#!/usr/bin/env python3
"""Tests for the `UserPromptExpansion` registries of pc-git and pc-kanban — run with: python3 -m pytest tests/test_direct_command_hooks.py"""

import argparse
import importlib.util
import json
import os
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
        input=json.dumps(
            {
                "command_args": command_args,
                "cwd": str(tmp_path),
                "session_id": "direct-command-session",
            }
        ),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "CLAUDE_PROJECT_DIR": str(tmp_path)},
    )
    assert proc.returncode == 0
    return proc.stdout


def test_registry_lists_the_direct_commands():
    matchers = sorted(m for _, m, _ in _entries())
    assert matchers == [
        "^pc-git:clean-gone$",
        "^pc-kanban:init$",
        "^pc-kanban:move-card$",
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


def _clean_gone_entry():
    (entry,) = [e for e in _entries() if e[1] == "^pc-git:clean-gone$"]
    return entry


def _git_init(path):
    path.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    return path


def test_clean_gone_absolute_repo_path_targets_that_repo(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    repo = _git_init(tmp_path / "sibling-repo")
    plugin, _, command = _clean_gone_entry()
    out = json.loads(_fire(plugin, command, str(repo), session))
    assert out["decision"] == "block"
    last = out["reason"].strip().splitlines()[-1]
    assert last.startswith("summary:")
    assert "1 nothing to clean" in last


def test_clean_gone_absolute_path_is_silent_unless_it_is_exactly_a_repo(tmp_path):
    repo = _git_init(tmp_path / "proj")
    _git_init(repo / "nested")
    plugin, _, command = _clean_gone_entry()
    assert (
        json.loads(_fire(plugin, command, str(repo), tmp_path))["decision"] == "block"
    )
    bad = [
        f"{repo}/../proj",
        f"{repo} --all",
        f"{repo}/\n",
        "//",
        "/",
        str(tmp_path),
        str(tmp_path / "missing"),
        f"{repo}/nested/../..",
    ]
    for arg in bad:
        assert _fire(plugin, command, arg, tmp_path) == "", arg


def test_clean_gone_grammar_rejects_dotdot_spaces_and_flags():
    plugin, _, command = _clean_gone_entry()
    argv = _argv(plugin, command)
    grammar = json.loads(argv[argv.index("--grammar") + 1])
    runner = _load_runner(Path(argv[1]))
    select = runner._select_argv
    assert select(grammar, ["/a/b"], "/") == ["--repo-path", "/a/b"]
    for tokens in (["/a/../b"], ["/a b"], ["-x"], ["/a", "/b"], ["--repo-path", "/a"]):
        assert select(grammar, tokens, "/") is None, tokens


@pytest.mark.parametrize(
    "tokens,expected",
    [
        (["select", "demo"], ["select", "demo"]),
        (["select", "0025/demo"], ["select", "0025/demo"]),
        (["select", "0025/demo", "--force"], ["select", "0025/demo", "--force"]),
        (["archive", "0025/demo.x-1"], ["archive", "0025/demo.x-1"]),
        (["drop", "0025/demo", "why", "not"], ["drop", "0025/demo", "why", "not"]),
        (["select", "25/demo"], None),
        (["select", "0025/"], None),
        (["select", "0025/a/b"], None),
        (["select", "../demo"], None),
    ],
)
def test_move_card_grammar_accepts_id_slash_slug(tokens, expected):
    command = next(c for _, m, c in _entries() if m == "^pc-kanban:move-card$")
    argv = _argv("pc-kanban", command)
    grammar = json.loads(argv[argv.index("--grammar") + 1])
    runner = _load_runner(Path(argv[1]))
    assert runner._select_argv(grammar, tokens, "/") == expected


def _load_runner(path):
    spec = importlib.util.spec_from_file_location("pc_git_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _child_session_id(tmp_path, event_extra):
    """What `CLAUDE_CODE_SESSION_ID` a script started by the runner sees for this event."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os\nprint(os.environ.get('CLAUDE_CODE_SESSION_ID', 'absent'))\n"
    )
    runner = _load_runner(
        PLUGINS / "pc-kanban" / "hooks" / "pc_kanban_direct_command_runner.py"
    )
    args = argparse.Namespace(
        script=str(probe),
        interpreter="python3",
        grammar=json.dumps({"forms": [{"match": [], "emit": []}]}),
        timeout=10.0,
    )
    return runner._run(args, {"command_args": "", "cwd": str(tmp_path), **event_extra})


def test_runner_forwards_the_event_session_id_to_the_child(tmp_path):
    assert _child_session_id(tmp_path, {"session_id": "sess-42"}) == "sess-42"


def test_runner_event_session_id_wins_over_the_ambient_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ambient")
    assert _child_session_id(tmp_path, {"session_id": "sess-42"}) == "sess-42"


def test_runner_leaves_the_environment_untouched_without_a_session_id(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert _child_session_id(tmp_path, {}) == "absent"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ambient")
    assert _child_session_id(tmp_path, {}) == "ambient"


@pytest.mark.parametrize("bad", ["", None, 7, ["x"]])
def test_runner_ignores_a_session_id_that_is_not_a_non_empty_string(
    tmp_path, monkeypatch, bad
):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert _child_session_id(tmp_path, {"session_id": bad}) == "absent"


@pytest.mark.parametrize("skill", ["commit", "commit-and-pr", "clean-gone"])
def test_git_skills_argument_hint_accepts_an_absolute_path(skill):
    text = (PLUGINS / "pc-git" / "skills" / skill / "SKILL.md").read_text()
    (hint,) = [ln for ln in text.splitlines() if ln.startswith("argument-hint:")]
    assert "/absolute/repo/path" in hint
