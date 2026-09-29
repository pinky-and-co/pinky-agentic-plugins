"""Tests for plugins/pc-autodiscovery/hooks/agents-md/ — AGENTS.md rendering, the SessionStart
hook that keeps the workspace and group files in sync, and the repo-level CLI.

Real filesystem under tmp_path; the hook also runs as a real subprocess.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK_DIR = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-autodiscovery"
    / "hooks"
    / "agents-md"
)
sys.path.insert(0, str(HOOK_DIR))

import agents_md_hook  # noqa: E402
import agents_md_repo  # noqa: E402
from agents_md_lib import (  # noqa: E402
    GROUP_HEADINGS,
    WORKSPACE_HEADINGS,
    has_foreign_sections,
    behavior_section,
    discover_groups,
    find_workspace_root,
    gotchas_section,
    packages_table,
    render_group_agents_md,
    render_standalone_agents_md,
    render_workspace_agents_md,
)


def _make_repo(path: Path, description: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    if description is not None:
        (path / "pyproject.toml").write_text(
            f'[project]\nname = "{path.name}"\ndescription = "{description}"\n'
        )


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    _make_repo(root / "solo-repo", description="A standalone package")
    _make_repo(root / "a-group" / "pkg-one", description="First package")
    _make_repo(root / "a-group" / "pkg-two")
    (root / "_not_a_repo").mkdir(parents=True)
    (root / "ws.code-workspace").write_text("{}")
    return root


def _profile(root: Path, data: dict) -> None:
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / "settings.pinky.json").write_text(json.dumps(data))


def _run_hook_main(root: Path, monkeypatch, capsys) -> str:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.delenv("PINKY_WORKSPACE_ROOT", raising=False)
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    assert agents_md_hook.run_hook() == 0
    return capsys.readouterr().out


# --- library: rendering -----------------------------------------------------


def test_packages_table_pinned(tmp_path):
    root = _workspace(tmp_path)
    assert packages_table(root) == (
        "| Package | Description | Path |\n"
        "|---|---|---|\n"
        "| a-group | pkg-one, pkg-two | `a-group/` |\n"
        "| solo-repo | A standalone package | `solo-repo/` |"
    )


def test_discover_groups_excludes_standalone_repos(tmp_path):
    assert [g.name for g in discover_groups(_workspace(tmp_path))] == ["a-group"]


def test_render_workspace_pinned_whole(tmp_path):
    root = _workspace(tmp_path)
    _profile(root, {"behavior": {"a": "**A** — first."}, "gotchas": ["Trap one."]})
    assert render_workspace_agents_md(root, None) == (
        "# AGENTS.md — workspace\n\n"
        "## Packages\n\n"
        "<!-- pinky:agents-packages:start -->\n\n" + packages_table(root) + "\n\n"
        "<!-- pinky:agents-packages:end -->\n\n"
        "## Cross-Package Patterns\n\n"
        "<!-- pinky:agents-cross-package-patterns:start -->\n\n"
        "AGENTS.md accumulates: root → repo, each level states only its own layer.\n\n"
        "<!-- pinky:agents-cross-package-patterns:end -->\n\n"
        "## Gotchas\n\n"
        "<!-- pinky:agents-gotchas:start -->\n\n- Trap one.\n\n"
        "<!-- pinky:agents-gotchas:end -->\n\n"
        "## Behavior policy\n\n"
        "<!-- pinky:agents-behavior:start -->\n\n- **A** — first.\n\n"
        "<!-- pinky:agents-behavior:end -->\n"
    )


def test_render_group_pinned_whole(tmp_path):
    root = _workspace(tmp_path)
    assert render_group_agents_md(root / "a-group", root, None) == (
        "# AGENTS.md — a-group\n\n"
        "## Packages\n\n"
        "<!-- pinky:agents-packages:start -->\n\n"
        "| Package | Description | Path |\n|---|---|---|\n"
        "| pkg-one | First package | `pkg-one/` |\n"
        "| pkg-two | — | `pkg-two/` |\n\n"
        "<!-- pinky:agents-packages:end -->\n\n"
        "## Cross-Package Patterns\n\n"
        "<!-- pinky:agents-cross-package-patterns:start -->\n\n"
        "AGENTS.md accumulates: root → repo, each level states only its own layer.\n\n"
        "<!-- pinky:agents-cross-package-patterns:end -->\n"
    )


def test_render_preserves_title_and_intro(tmp_path):
    root = _workspace(tmp_path)
    existing = "# AGENTS.md — workspace\nHand-typed intro.\n\n## Packages\n\nstale\n"
    assert render_workspace_agents_md(root, existing).startswith(
        "# AGENTS.md — workspace\nHand-typed intro.\n\n## Packages"
    )
    group_existing = "# Custom title\nIntro.\n\n## Packages\n\nstale\n"
    assert render_group_agents_md(root / "a-group", root, group_existing).startswith(
        "# Custom title\nIntro.\n\n## Packages"
    )


def test_render_is_idempotent(tmp_path):
    root = _workspace(tmp_path)
    first = render_workspace_agents_md(root, None)
    assert render_workspace_agents_md(root, first) == first


def test_behavior_section_in_file_order(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    _profile(root, {"behavior": {"a": "**A** — first.", "b": "**B** — second."}})
    assert behavior_section(root) == "- **A** — first.\n- **B** — second."


def test_behavior_section_empty_when_profile_missing_or_invalid(tmp_path):
    assert behavior_section(tmp_path / "nowhere") == ""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.pinky.json").write_text("{not json")
    assert behavior_section(tmp_path) == ""


def test_gotchas_section_renders_dict_and_list(tmp_path):
    _profile(tmp_path, {"gotchas": {"x": "First.", "y": "Second."}})
    assert gotchas_section(tmp_path) == "- First.\n- Second."
    _profile(tmp_path, {"gotchas": ["Alpha.", "Beta."]})
    assert gotchas_section(tmp_path) == "- Alpha.\n- Beta."


def test_gotchas_section_empty_when_key_absent(tmp_path):
    _profile(tmp_path, {"behavior": {"a": "x"}})
    assert gotchas_section(tmp_path) == ""
    root = _workspace(tmp_path)
    assert (
        "<!-- pinky:agents-gotchas:start -->\n\n\n\n<!-- pinky:agents-gotchas:end -->"
        in (render_workspace_agents_md(root, None))
    )


def test_unreadable_pyproject_yields_dash(tmp_path):
    root = _workspace(tmp_path)
    (root / "solo-repo" / "pyproject.toml").write_text("this is [not toml")
    assert "| solo-repo | — | `solo-repo/` |" in packages_table(root)
    (root / "solo-repo" / "pyproject.toml").unlink()
    assert "| solo-repo | — | `solo-repo/` |" in packages_table(root)


# --- library: standalone ----------------------------------------------------


def _repo_with_body(root: Path) -> Path:
    repo = root / "a-group" / "pkg-one"
    (repo / "AGENTS.md").write_text(
        "# AGENTS.md — pkg-one\n\n## Naming glossary\n\n`pkg` = short name.\n\n"
        "## Repo conventions\n\nRuff only.\n",
        encoding="utf-8",
    )
    return repo


def test_render_standalone_pinned_whole(tmp_path):
    root = _workspace(tmp_path)
    _profile(root, {"gotchas": ["Trap."], "behavior": ["Rule."]})
    assert render_standalone_agents_md(root / "a-group" / "pkg-one", root, None) == (
        "# AGENTS.md — pkg-one\n\n"
        "## Gotchas\n\n<!-- pinky:agents-gotchas:start -->\n\n- Trap.\n\n"
        "<!-- pinky:agents-gotchas:end -->\n\n"
        "## Behavior policy\n\n<!-- pinky:agents-behavior:start -->\n\n- Rule.\n\n"
        "<!-- pinky:agents-behavior:end -->\n\n"
        "## Naming glossary\n\n_Not written yet._\n\n"
        "## Repo conventions\n\n_Not written yet._\n"
    )


def test_render_standalone_drops_packages(tmp_path):
    root = _workspace(tmp_path)
    rendered = render_standalone_agents_md(root / "a-group" / "pkg-one", root, None)
    assert "Packages" not in rendered
    assert "Cross-Package Patterns" not in rendered


def test_standalone_write_is_idempotent_and_keeps_body(tmp_path):
    root = _workspace(tmp_path)
    repo = _repo_with_body(root)
    agents_md_repo.write_standalone_agents_md(repo, root)
    first = (repo / "AGENTS.md").read_text(encoding="utf-8")
    agents_md_repo.write_standalone_agents_md(repo, root)
    second = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert first == second
    assert second.count("Ruff only.") == 1
    assert second.count("## Gotchas") == 1


# --- repo CLI ---------------------------------------------------------------


def test_repo_seed_folds_context_md_and_deletes_it(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    (repo / "CONTEXT.md").write_text(
        "# Ctx\n\nSingle source of truth.\n\n`foo` = a thing.\n"
    )
    created = agents_md_repo.seed_repo_agents_md(repo)
    assert created == repo / "AGENTS.md"
    assert created.read_text(encoding="utf-8") == (
        "# AGENTS.md — repo\n\n## Naming glossary\n\n"
        "Single source of truth.\n\n`foo` = a thing.\n\n"
        "## Repo conventions\n\n_Not written yet._\n"
    )
    assert not (repo / "CONTEXT.md").exists()


def test_repo_seed_never_overwrites(tmp_path, capsys):
    repo = tmp_path / "repo"
    _make_repo(repo)
    (repo / "AGENTS.md").write_text("# hand-typed\n")
    assert agents_md_repo.seed_repo_agents_md(repo) is None
    assert agents_md_repo.main([str(repo)]) == 0
    assert capsys.readouterr().out.startswith("exists, left untouched")
    assert (repo / "AGENTS.md").read_text() == "# hand-typed\n"


def test_repo_cli_created_line_and_no_symlink(tmp_path, capsys):
    repo = tmp_path / "repo"
    _make_repo(repo)
    assert agents_md_repo.main([str(repo)]) == 0
    assert capsys.readouterr().out.startswith("created ")
    assert not (repo / "CLAUDE.md").exists()


def test_repo_cli_standalone_without_workspace_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PINKY_WORKSPACE_ROOT", raising=False)
    repo = tmp_path / "repo"
    _make_repo(repo)
    assert agents_md_repo.main([str(repo), "--standalone"]) == 1
    assert "workspace not resolvable" in capsys.readouterr().err
    assert not (repo / "AGENTS.md").exists()


def test_repo_cli_standalone_with_workspace_flag(tmp_path):
    root = _workspace(tmp_path)
    _profile(root, {"gotchas": ["Trap."]})
    repo = root / "a-group" / "pkg-one"
    assert (
        agents_md_repo.main([str(repo), "--standalone", "--workspace", str(root)]) == 0
    )
    assert "- Trap." in (repo / "AGENTS.md").read_text(encoding="utf-8")


# --- find_workspace_root ----------------------------------------------------


def test_find_workspace_root_walks_up_to_marker(tmp_path):
    root = _workspace(tmp_path)
    assert find_workspace_root(root / "a-group" / "pkg-one", {}) == root.resolve()


def test_find_workspace_root_env_wins_over_marker(tmp_path):
    root = _workspace(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    env = {"PINKY_WORKSPACE_ROOT": str(other)}
    assert find_workspace_root(root, env) == other


def test_find_workspace_root_ignores_env_when_not_a_directory(tmp_path):
    root = _workspace(tmp_path)
    env = {"PINKY_WORKSPACE_ROOT": str(tmp_path / "missing")}
    assert find_workspace_root(root, env) == root.resolve()


def test_find_workspace_root_none_outside_any_workspace(tmp_path):
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    assert find_workspace_root(lonely, {}) is None


# --- hook -------------------------------------------------------------------


def test_hook_writes_workspace_and_groups_after_description_change(
    tmp_path, monkeypatch, capsys
):
    root = _workspace(tmp_path)
    out = _run_hook_main(root, monkeypatch, capsys)
    payload = json.loads(out)
    assert payload["systemMessage"] == "agents-md: rewrote AGENTS.md, a-group/AGENTS.md"
    assert payload["hookSpecificOutput"] == {
        "hookEventName": "SessionStart",
        "additionalContext": payload["systemMessage"],
    }
    (root / "solo-repo" / "pyproject.toml").write_text(
        '[project]\nname = "solo-repo"\ndescription = "Changed"\n'
    )
    (root / "a-group" / "pkg-two" / "pyproject.toml").write_text(
        '[project]\nname = "pkg-two"\ndescription = "Now described"\n'
    )
    out = _run_hook_main(root, monkeypatch, capsys)
    assert (
        json.loads(out)["systemMessage"]
        == "agents-md: rewrote AGENTS.md, a-group/AGENTS.md"
    )
    assert "| solo-repo | Changed |" in (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "| pkg-two | Now described |" in (root / "a-group" / "AGENTS.md").read_text(
        encoding="utf-8"
    )


def test_hook_second_run_writes_nothing_and_prints_nothing(
    tmp_path, monkeypatch, capsys
):
    root = _workspace(tmp_path)
    _run_hook_main(root, monkeypatch, capsys)
    paths = [root / "AGENTS.md", root / "a-group" / "AGENTS.md"]
    for p in paths:
        os.utime(p, (1_000_000, 1_000_000))
    assert _run_hook_main(root, monkeypatch, capsys) == ""
    assert [p.stat().st_mtime for p in paths] == [1_000_000, 1_000_000]


def test_hook_preserves_hand_written_intro(tmp_path, monkeypatch, capsys):
    root = _workspace(tmp_path)
    (root / "AGENTS.md").write_text("# Mine\nKeep me.\n\n## Packages\n\nstale\n")
    _run_hook_main(root, monkeypatch, capsys)
    assert (
        (root / "AGENTS.md")
        .read_text(encoding="utf-8")
        .startswith("# Mine\nKeep me.\n\n## Packages")
    )


def test_hook_without_workspace_writes_nothing(tmp_path, monkeypatch, capsys):
    lonely = tmp_path / "lonely"
    _make_repo(lonely / "grp" / "r1")
    _make_repo(lonely / "grp" / "r2")
    assert _run_hook_main(lonely, monkeypatch, capsys) == ""
    assert not (lonely / "AGENTS.md").exists()
    assert not (lonely / "grp" / "AGENTS.md").exists()


def test_hook_env_root_wins_over_marker(tmp_path, monkeypatch, capsys):
    root = _workspace(tmp_path)
    other = tmp_path / "other"
    _make_repo(other / "only-repo", description="Elsewhere")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("PINKY_WORKSPACE_ROOT", str(other))
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    agents_md_hook.run_hook()
    capsys.readouterr()
    assert (other / "AGENTS.md").is_file()
    assert not (root / "AGENTS.md").exists()


def test_hook_unreadable_pyproject_still_writes_every_file(
    tmp_path, monkeypatch, capsys
):
    root = _workspace(tmp_path)
    (root / "solo-repo" / "pyproject.toml").write_bytes(b"\xff\xfe not toml")
    (root / "a-group" / "pkg-one" / "pyproject.toml").unlink()
    (root / "a-group" / "pkg-one" / "pyproject.toml").mkdir()
    _run_hook_main(root, monkeypatch, capsys)
    assert "| solo-repo | — |" in (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "| pkg-one | — |" in (root / "a-group" / "AGENTS.md").read_text(
        encoding="utf-8"
    )


def test_hook_write_failure_on_one_file_does_not_stop_others(
    tmp_path, monkeypatch, capsys
):
    root = _workspace(tmp_path)
    _make_repo(root / "b-group" / "pkg-three")
    real = agents_md_hook.write_if_changed

    def flaky(path: Path, content: str) -> bool:
        if path.parent.name == "a-group":
            raise OSError("disk full")
        return real(path, content)

    monkeypatch.setattr(agents_md_hook, "write_if_changed", flaky)
    out = _run_hook_main(root, monkeypatch, capsys)
    assert (
        json.loads(out)["systemMessage"]
        == "agents-md: rewrote AGENTS.md, b-group/AGENTS.md"
    )
    assert not (root / "a-group" / "AGENTS.md").exists()


def test_hook_never_touches_claude_md(tmp_path, monkeypatch, capsys):
    root = _workspace(tmp_path)
    (root / "AGENTS.md").write_text("# AGENTS.md — workspace\n\n## Packages\n\nstale\n")
    (root / "CLAUDE.md").symlink_to("AGENTS.md")
    (root / "a-group" / "CLAUDE.md").write_bytes(b"hand-made\r\nbytes\n")
    out = _run_hook_main(root, monkeypatch, capsys)
    assert json.loads(out)["systemMessage"].startswith("agents-md: rewrote AGENTS.md")
    assert (root / "a-group" / "AGENTS.md").is_file()
    assert "stale" not in (root / "AGENTS.md").read_text(encoding="utf-8")
    assert (root / "CLAUDE.md").is_symlink()
    assert os.readlink(root / "CLAUDE.md") == "AGENTS.md"
    assert not (root / "a-group" / "CLAUDE.md").is_symlink()
    assert (root / "a-group" / "CLAUDE.md").read_bytes() == b"hand-made\r\nbytes\n"
    assert not (root / "solo-repo" / "CLAUDE.md").exists()


def test_hook_tolerates_malformed_event_and_uses_event_cwd(tmp_path):
    root = _workspace(tmp_path)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    script = HOOK_DIR / "agents_md_hook.py"
    bad = subprocess.run(
        [sys.executable, str(script)],
        input="{oops",
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (bad.returncode, bad.stdout) == (0, "")
    good = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps({"cwd": str(root)}),
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert good.returncode == 0
    assert (root / "AGENTS.md").is_file()


def test_hook_subprocess_works_without_pinky_on_path(tmp_path):
    root = _workspace(tmp_path)
    env = {
        "PATH": "/usr/bin:/bin",
        "CLAUDE_PROJECT_DIR": str(root),
        "HOME": str(tmp_path),
    }
    result = subprocess.run(
        [sys.executable, str(HOOK_DIR / "agents_md_hook.py")],
        input="{}",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["systemMessage"] == (
        "agents-md: rewrote AGENTS.md, a-group/AGENTS.md"
    )
    assert (root / "a-group" / "AGENTS.md").is_file()


def test_hook_leaves_no_temp_files_behind(tmp_path, monkeypatch, capsys):
    root = _workspace(tmp_path)
    _run_hook_main(root, monkeypatch, capsys)
    assert not list(root.glob(".agents-md-*")) and not list(
        (root / "a-group").glob(".agents-md-*")
    )


# --- workspace root safety --------------------------------------------------


@pytest.mark.parametrize("git_kind", ["dir", "file"])
def test_hook_ignores_repo_shipping_its_own_workspace_file(
    tmp_path, monkeypatch, capsys, git_kind
):
    proj = tmp_path / "proj"
    _make_repo(proj / "sub")
    if git_kind == "dir":
        (proj / ".git").mkdir()
    else:
        (proj / ".git").write_text("gitdir: ../elsewhere\n")
    (proj / "own.code-workspace").write_text("{}")
    (proj / "AGENTS.md").write_bytes(b"# Hand written\n\nDo not touch.\n")
    assert _run_hook_main(proj, monkeypatch, capsys) == ""
    assert (proj / "AGENTS.md").read_bytes() == b"# Hand written\n\nDo not touch.\n"
    assert not (proj / "sub" / "AGENTS.md").exists()
    assert find_workspace_root(proj / "sub", {}) is None


def test_hook_ignores_stray_marker_in_home(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    proj = home / "proj"
    _make_repo(proj / "sub")
    (home / "stray.code-workspace").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    assert _run_hook_main(proj, monkeypatch, capsys) == ""
    assert not (home / "AGENTS.md").exists()
    assert find_workspace_root(proj, {"PINKY_WORKSPACE_ROOT": str(home)}) is None


def test_find_workspace_root_rejects_explicit_git_repo(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    assert find_workspace_root(tmp_path, {"PINKY_WORKSPACE_ROOT": str(repo)}) is None


# --- glossary folding --------------------------------------------------------


def _seed_with_context(tmp_path: Path, context: str) -> str:
    repo = tmp_path / "repo"
    _make_repo(repo)
    (repo / "CONTEXT.md").write_text(context)
    agents_md_repo.seed_repo_agents_md(repo)
    return (repo / "AGENTS.md").read_text(encoding="utf-8")


def test_context_fold_title_and_one_paragraph_keeps_the_paragraph(tmp_path):
    written = _seed_with_context(tmp_path, "# Ctx\n\n`foo` = a thing.\n")
    assert written == (
        "# AGENTS.md — repo\n\n## Naming glossary\n\n`foo` = a thing.\n\n"
        "## Repo conventions\n\n_Not written yet._\n"
    )
    assert not (tmp_path / "repo" / "CONTEXT.md").exists()


def test_context_fold_title_only_uses_placeholder(tmp_path):
    written = _seed_with_context(tmp_path, "# Ctx\n")
    assert "## Naming glossary\n\n_Not written yet._" in written


def test_context_fold_keeps_everything_but_the_title_line(tmp_path):
    written = _seed_with_context(tmp_path, "# T\n- term: def\n- term2: def\n\nB\n\nC\n")
    assert written == (
        "# AGENTS.md — repo\n\n## Naming glossary\n\n"
        "- term: def\n- term2: def\n\nB\n\nC\n\n"
        "## Repo conventions\n\n_Not written yet._\n"
    )
    assert not (tmp_path / "repo" / "CONTEXT.md").exists()


def test_context_fold_keeps_context_md_when_its_text_is_not_fully_written(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _make_repo(repo)
    (repo / "CONTEXT.md").write_text("# T\nGlossary body.\n")
    monkeypatch.setattr(
        agents_md_repo, "_repo_default_body", lambda r: "## Naming glossary\n\nother"
    )
    agents_md_repo.seed_repo_agents_md(repo)
    assert (repo / "CONTEXT.md").is_file()


def test_context_fold_write_failure_leaves_context_md(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    repo = tmp_path / "repo"
    _make_repo(repo)
    (repo / "CONTEXT.md").write_text("# Ctx\n\nLead.\n\n`a` = 1.\n")
    repo.chmod(0o500)
    try:
        with pytest.raises(OSError):
            agents_md_repo.seed_repo_agents_md(repo)
    finally:
        repo.chmod(0o700)
    assert (repo / "CONTEXT.md").is_file()
    assert not (repo / "AGENTS.md").exists()


# --- python without tomllib / broken import ---------------------------------


def _run_hook_script(code_prefix: str, root: Path, tmp_path: Path):
    script = HOOK_DIR / "agents_md_hook.py"
    code = (
        f"import sys, runpy; {code_prefix}; "
        f"runpy.run_path({str(script)!r}, run_name='__main__')"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "CLAUDE_PROJECT_DIR": str(root),
        "HOME": str(tmp_path),
    }
    return subprocess.run(
        [sys.executable, "-c", code],
        input="{}",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_hook_without_tomllib_does_nothing(tmp_path):
    root = _workspace(tmp_path)
    result = _run_hook_script("sys.modules['tomllib'] = None", root, tmp_path)
    assert (result.returncode, result.stdout) == (0, "")
    assert not (root / "AGENTS.md").exists()
    assert not (root / "a-group" / "AGENTS.md").exists()


def test_hook_exits_silently_when_the_library_cannot_be_imported(tmp_path):
    root = _workspace(tmp_path)
    result = _run_hook_script("sys.modules['agents_md_lib'] = None", root, tmp_path)
    assert (result.returncode, result.stdout) == (0, "")
    assert not (root / "AGENTS.md").exists()


def test_hook_runs_under_the_system_python(tmp_path):
    system_python = "/usr/bin/python3"
    if not os.path.exists(system_python):
        pytest.skip("no system python")
    probe = subprocess.run(
        [system_python, "--version"], capture_output=True, text=True, check=False
    )
    if probe.returncode != 0:
        pytest.skip("system python is not usable")
    root = _workspace(tmp_path)
    env = {
        "PATH": "/usr/bin:/bin",
        "CLAUDE_PROJECT_DIR": str(root),
        "HOME": str(tmp_path),
    }
    result = subprocess.run(
        [system_python, str(HOOK_DIR / "agents_md_hook.py")],
        input="{}",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    has_tomllib = subprocess.run(
        [system_python, "-c", "import tomllib"], capture_output=True, check=False
    )
    assert result.returncode == 0
    assert (root / "AGENTS.md").is_file() == (has_tomllib.returncode == 0)
    if has_tomllib.returncode != 0:
        assert result.stdout == ""


# --- intro and hand-written sections ----------------------------------------


def test_intro_without_any_heading_keeps_the_whole_text(tmp_path):
    root = _workspace(tmp_path)
    rendered = render_workspace_agents_md(root, "Only free text.\nSecond line.\n")
    assert rendered.startswith("Only free text.\nSecond line.\n\n## Packages")


def test_intro_of_file_starting_with_heading_gets_default_title(tmp_path):
    root = _workspace(tmp_path)
    rendered = render_workspace_agents_md(root, "## Packages\n\nstale\n")
    assert rendered.startswith("# AGENTS.md — workspace\n\n## Packages")


def test_unclosed_code_fence_in_intro_never_makes_the_file_grow(tmp_path):
    root = _workspace(tmp_path)
    first = render_workspace_agents_md(root, "# T\n```\nunclosed\n")
    second = render_workspace_agents_md(root, first)
    third = render_workspace_agents_md(root, second)
    assert first == second == third
    assert first.startswith("# T\n```\nunclosed\n\n## Packages")


def test_intro_stops_at_the_first_generator_marker(tmp_path):
    root = _workspace(tmp_path)
    existing = "# T\nIntro.\n<!-- pinky:agents-packages:start -->\nold\n"
    assert render_workspace_agents_md(root, existing).startswith(
        "# T\nIntro.\n\n## Packages"
    )


def test_standalone_with_marker_but_no_gotchas_heading_keeps_intro(tmp_path):
    root = _workspace(tmp_path)
    existing = (
        "# Custom title\nIntro.\n\n## Behavior policy\n\n"
        "<!-- pinky:agents-behavior:start -->\n\n\n\n"
        "<!-- pinky:agents-behavior:end -->\n\n"
        "## Body\n\nkeep\n"
    )
    rendered = render_standalone_agents_md(root / "a-group" / "pkg-one", root, existing)
    assert rendered.startswith("# Custom title\nIntro.\n\n## Gotchas")
    assert rendered.endswith("<!-- pinky:agents-behavior:end -->\n\n## Body\n\nkeep\n")


def test_hook_reports_dropped_hand_written_section_once(tmp_path, monkeypatch, capsys):
    root = _workspace(tmp_path)
    _run_hook_main(root, monkeypatch, capsys)
    text = (root / "AGENTS.md").read_text(encoding="utf-8")
    (root / "AGENTS.md").write_text(text + "\n## My notes\n\nhand written\n")
    out = _run_hook_main(root, monkeypatch, capsys)
    assert json.loads(out)["systemMessage"] == (
        "agents-md: rewrote AGENTS.md; "
        "sections outside the generated blocks are replaced in AGENTS.md"
    )
    assert "My notes" not in (root / "AGENTS.md").read_text(encoding="utf-8")
    assert _run_hook_main(root, monkeypatch, capsys) == ""


# --- atomic write -----------------------------------------------------------


def test_hook_leaves_symlinked_agents_md_untouched(tmp_path, monkeypatch, capsys):
    root = _workspace(tmp_path)
    other = tmp_path / "other.md"
    other.write_text("keep\n")
    (root / "AGENTS.md").symlink_to(other)
    out = _run_hook_main(root, monkeypatch, capsys)
    assert json.loads(out)["systemMessage"] == "agents-md: rewrote a-group/AGENTS.md"
    assert (root / "AGENTS.md").is_symlink()
    assert os.readlink(root / "AGENTS.md") == str(other)
    assert other.read_text() == "keep\n"


def test_write_if_changed_preserves_existing_mode_and_leaves_no_temp(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text("old\n")
    target.chmod(0o640)
    assert agents_md_hook.write_if_changed(target, "new\n") is True
    assert target.read_text() == "new\n"
    assert target.stat().st_mode & 0o777 == 0o640
    assert list(tmp_path.glob(".agents-md-*")) == []


def test_write_if_changed_new_file_gets_umask_default_mode(tmp_path):
    umask = os.umask(0)
    os.umask(umask)
    target = tmp_path / "AGENTS.md"
    assert agents_md_hook.write_if_changed(target, "x\n") is True
    assert target.stat().st_mode & 0o777 == 0o666 & ~umask


def test_write_if_changed_failure_removes_temp_file(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.mkdir()
    with pytest.raises(OSError):
        agents_md_hook.write_if_changed(target, "x\n")
    assert list(tmp_path.glob(".agents-md-*")) == []


# --- repo discovery ---------------------------------------------------------


def test_same_named_repos_in_different_groups_both_appear(tmp_path):
    root = tmp_path / "workspace"
    _make_repo(root / "g1" / "shared")
    _make_repo(root / "g2" / "shared")
    assert packages_table(root) == (
        "| Package | Description | Path |\n|---|---|---|\n"
        "| g1 | shared | `g1/` |\n| g2 | shared | `g2/` |"
    )
    assert [g.name for g in discover_groups(root)] == ["g1", "g2"]


def test_symlinked_directories_are_not_followed(tmp_path):
    root = _workspace(tmp_path)
    (root / "linked").symlink_to(root / "a-group")
    (root / "a-group" / "alias").symlink_to(root / "a-group" / "pkg-one")
    table = packages_table(root)
    assert "linked" not in table
    assert "alias" not in table
    assert "| a-group | pkg-one, pkg-two |" in table


def test_dependency_directories_are_skipped(tmp_path):
    root = _workspace(tmp_path)
    _make_repo(root / "node_modules" / "dep")
    _make_repo(root / "vendor")
    _make_repo(root / "a-group" / "site-packages")
    table = packages_table(root)
    assert "node_modules" not in table
    assert "vendor" not in table
    assert "site-packages" not in table
    assert [g.name for g in discover_groups(root)] == ["a-group"]


# --- sanitisation -----------------------------------------------------------


def test_multiline_description_stays_one_table_row(tmp_path):
    root = _workspace(tmp_path)
    (root / "solo-repo" / "pyproject.toml").write_text(
        '[project]\nname = "solo-repo"\n'
        'description = "line one | pipe\\n## Heading\\n<!-- hidden --> tail"\n'
    )
    rendered = render_workspace_agents_md(root, None)
    assert (
        "| solo-repo | line one \\| pipe ## Heading tail | `solo-repo/` |" in rendered
    )
    assert "\n## Heading" not in rendered
    assert "hidden" not in rendered


def test_long_description_is_truncated(tmp_path):
    root = _workspace(tmp_path)
    (root / "solo-repo" / "pyproject.toml").write_text(
        f'[project]\nname = "solo-repo"\ndescription = "{"a" * 400}"\n'
    )
    assert f"| solo-repo | {'a' * 299}… |" in packages_table(root)


def test_profile_values_lose_html_comments(tmp_path):
    _profile(tmp_path, {"gotchas": ["ok <!-- pinky:agents-gotchas:end --> more"]})
    assert gotchas_section(tmp_path) == "- ok more"


def test_marker_text_in_gotcha_value_does_not_grow_the_standalone_body(tmp_path):
    root = _workspace(tmp_path)
    _profile(root, {"gotchas": ["x <!-- pinky:agents-behavior:end --> y"]})
    repo = _repo_with_body(root)
    agents_md_repo.write_standalone_agents_md(repo, root)
    first = (repo / "AGENTS.md").read_text(encoding="utf-8")
    agents_md_repo.write_standalone_agents_md(repo, root)
    agents_md_repo.write_standalone_agents_md(repo, root)
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == first
    assert first.count("Ruff only.") == 1


# --- repo CLI safety --------------------------------------------------------


def test_repo_cli_refuses_a_directory_without_git(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert agents_md_repo.main([str(plain)]) == 1
    assert "not a git repo" in capsys.readouterr().err
    assert not (plain / "AGENTS.md").exists()


def test_repo_cli_accepts_double_dash_and_resolves_relative_paths(
    tmp_path, monkeypatch, capsys
):
    _make_repo(tmp_path / "repo")
    monkeypatch.chdir(tmp_path)
    assert agents_md_repo.main(["--", "repo"]) == 0
    expected = (tmp_path / "repo").resolve() / "AGENTS.md"
    assert capsys.readouterr().out == f"created {expected}\n"


def test_repo_cli_refuses_workspace_that_is_a_repo_or_missing(tmp_path, capsys):
    repo = tmp_path / "repo"
    _make_repo(repo)
    other = tmp_path / "other"
    _make_repo(other)
    for bad in (other, tmp_path / "missing"):
        argv = [str(repo), "--standalone", "--workspace", str(bad)]
        assert agents_md_repo.main(argv) == 1
        assert "invalid workspace" in capsys.readouterr().err
    assert not (repo / "AGENTS.md").exists()


def test_repo_cli_standalone_refuses_symlinked_agents_md(tmp_path, capsys):
    root = _workspace(tmp_path)
    repo = root / "a-group" / "pkg-one"
    other = tmp_path / "other.md"
    other.write_text("keep\n")
    (repo / "AGENTS.md").symlink_to(other)
    argv = [str(repo), "--standalone", "--workspace", str(root)]
    assert agents_md_repo.main(argv) == 1
    assert "symlink" in capsys.readouterr().err
    assert other.read_text() == "keep\n"


def test_repo_seed_treats_dangling_symlink_as_existing(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    (repo / "AGENTS.md").symlink_to(tmp_path / "nowhere.md")
    assert agents_md_repo.seed_repo_agents_md(repo) is None
    assert not (tmp_path / "nowhere.md").exists()


def test_repo_standalone_write_is_atomic_and_leaves_no_temp(tmp_path):
    root = _workspace(tmp_path)
    repo = _repo_with_body(root)
    agents_md_repo.write_standalone_agents_md(repo, root)
    assert list(repo.glob(".agents-md-*")) == []


# --- idempotence, silent loss, sanitisation ---------------------------------


def test_profile_value_whitespace_collapses_and_second_run_writes_nothing(
    tmp_path, monkeypatch, capsys
):
    root = _workspace(tmp_path)
    _profile(root, {"gotchas": ["a\r\nb\n## H"], "behavior": ["x\n\n## Y\tz"]})
    rendered = render_workspace_agents_md(root, None)
    assert "- a b ## H\n" in rendered
    assert "- x ## Y z\n" in rendered
    assert "\r" not in rendered
    assert "\n## H" not in rendered
    assert render_workspace_agents_md(root, rendered) == rendered
    assert _run_hook_main(root, monkeypatch, capsys) != ""
    assert _run_hook_main(root, monkeypatch, capsys) == ""


def test_strip_comments_leaves_no_partial_delimiter(tmp_path):
    _profile(tmp_path, {"gotchas": ["<<!--!-- a --!>>"]})
    section = gotchas_section(tmp_path)
    assert "<!--" not in section
    assert "-->" not in section


def test_directory_names_are_escaped_in_table_cells(tmp_path):
    root = tmp_path / "workspace"
    _make_repo(root / "we|ird" / "pkg")
    assert packages_table(root) == (
        "| Package | Description | Path |\n|---|---|---|\n"
        "| we\\|ird | pkg | `we\\|ird/` |"
    )


def _generated_workspace(tmp_path: Path) -> tuple[Path, str]:
    root = _workspace(tmp_path)
    return root, render_workspace_agents_md(root, None)


def test_lossy_detection_flags_bullets_under_a_heading_outside_markers(tmp_path):
    _, generated = _generated_workspace(tmp_path)
    hand = "# T\n\n## Gotchas\n\n- hand bullet\n"
    assert has_foreign_sections(hand, WORKSPACE_HEADINGS) is True
    assert has_foreign_sections(generated, WORKSPACE_HEADINGS) is False


def test_lossy_detection_flags_text_after_the_last_end_marker(tmp_path):
    _, generated = _generated_workspace(tmp_path)
    assert (
        has_foreign_sections(generated + "trailing text\n", WORKSPACE_HEADINGS) is True
    )


def test_lossy_detection_flags_subsections_and_markerless_content(tmp_path):
    _, generated = _generated_workspace(tmp_path)
    assert has_foreign_sections(generated + "\n### Sub\n", WORKSPACE_HEADINGS) is True
    assert (
        has_foreign_sections("# T\n\n## Packages\n\nstale\n", WORKSPACE_HEADINGS)
        is True
    )
    assert has_foreign_sections("# T\nOnly an intro.\n", WORKSPACE_HEADINGS) is False


def test_lossy_detection_group_level_heading_set(tmp_path):
    root = _workspace(tmp_path)
    group = render_group_agents_md(root / "a-group", root, None)
    assert has_foreign_sections(group, GROUP_HEADINGS) is False
    assert has_foreign_sections(group + "\n## Gotchas\n", GROUP_HEADINGS) is True


@pytest.mark.parametrize(
    "name", ["agents_md_hook.py", "agents_md_lib.py", "agents_md_repo.py"]
)
def test_hook_modules_parse_as_python_3_9(name):
    source = (HOOK_DIR / name).read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 9))


# --- line-anchored markers, delete guard, lazy body -------------------------


def test_inline_marker_mention_in_intro_is_kept_and_renders_are_stable(tmp_path):
    root = _workspace(tmp_path)
    existing = (
        "# T\nSee <!-- pinky:agents-packages:start --> for the format.\n\n"
        "## Packages\n\nstale\n"
    )
    first = render_workspace_agents_md(root, existing)
    second = render_workspace_agents_md(root, first)
    third = render_workspace_agents_md(root, second)
    assert first.startswith(
        "# T\nSee <!-- pinky:agents-packages:start --> for the format.\n\n## Packages"
    )
    assert first == second == third


def test_lossy_detection_flags_hand_text_between_real_blocks(tmp_path):
    _, generated = _generated_workspace(tmp_path)
    marker = "<!-- pinky:agents-packages:end -->\n"
    tampered = generated.replace(marker, marker + "\nhand text between blocks\n", 1)
    assert tampered != generated
    assert has_foreign_sections(tampered, WORKSPACE_HEADINGS) is True
    assert has_foreign_sections(generated, WORKSPACE_HEADINGS) is False


@pytest.mark.parametrize("context", ["# Only a title\n", "", "  \n\n"])
def test_context_md_without_glossary_is_never_deleted(tmp_path, context):
    repo = tmp_path / "repo"
    _make_repo(repo)
    (repo / "CONTEXT.md").write_text(context)
    assert agents_md_repo.seed_repo_agents_md(repo) == repo / "AGENTS.md"
    assert (repo / "CONTEXT.md").is_file()


def test_standalone_ignores_unreadable_context_md_when_body_exists(tmp_path):
    root = _workspace(tmp_path)
    repo = _repo_with_body(root)
    (repo / "CONTEXT.md").write_bytes(b"\xff\xfe not utf-8")
    agents_md_repo.write_standalone_agents_md(repo, root)
    assert "Ruff only." in (repo / "AGENTS.md").read_text(encoding="utf-8")
