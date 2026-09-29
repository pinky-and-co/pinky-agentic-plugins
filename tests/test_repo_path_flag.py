#!/usr/bin/env python3
"""Tests for `--repo-path` of bulk_commit.sh and bulk_clean_gone.sh — one repo, exactly."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "pc-git"
BULK_COMMIT = PLUGIN / "hooks" / "branch-policy" / "bulk_commit.sh"
CLEAN_GONE = PLUGIN / "skills" / "clean-gone" / "scripts" / "bulk_clean_gone.sh"


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _run(script: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=_env(),
    )


def _repo(path: Path, dirty: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=_env())
    if dirty:
        (path / "untracked.txt").write_text("x\n")
    return path


def _staged(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "diff", "--staged", "--name-only"],
        capture_output=True,
        text=True,
        env=_env(),
    ).stdout


def test_discover_ignores_nested_repo_with_same_basename(tmp_path):
    outer = _repo(tmp_path / "work" / "proj")
    _repo(outer / "vendor" / "proj")
    res = _run(BULK_COMMIT, tmp_path, "discover", "--repo-path", str(outer))
    assert res.returncode == 0
    assert res.stdout == "proj\n"


def test_plan_stages_only_the_outer_repo(tmp_path):
    outer = _repo(tmp_path / "proj")
    inner = _repo(outer / "vendor" / "proj")
    subprocess.run(
        ["git", "-C", str(inner), "-c", "user.name=t", "-c", "user.email=t@t"]
        + ["commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
        env=_env(),
    )
    res = _run(BULK_COMMIT, tmp_path, "plan", "--repo-path", str(outer))
    assert res.returncode == 0, res.stderr
    assert "untracked.txt" in _staged(outer)
    assert _staged(inner) == ""


def test_container_with_top_level_git_is_only_itself(tmp_path):
    container = _repo(tmp_path / "container", dirty=False)
    _repo(container / "a")
    _repo(container / "b")
    res = _run(BULK_COMMIT, tmp_path, "discover", "--repo-path", str(container))
    assert res.returncode == 0
    assert res.stdout == "container\n"


def test_sibling_path_works_while_root_default_is_elsewhere(tmp_path):
    elsewhere = tmp_path / "session"
    elsewhere.mkdir()
    sibling = _repo(tmp_path / "sibling")
    res = _run(BULK_COMMIT, elsewhere, "discover", "--repo-path", str(sibling))
    assert (res.returncode, res.stdout) == (0, "sibling\n")
    res = _run(CLEAN_GONE, elsewhere, "--repo-path", str(sibling))
    assert res.returncode == 0
    assert res.stdout.strip().splitlines()[-1].startswith("summary:")
    assert "1 nothing to clean" in res.stdout


def test_worktree_style_dot_git_file_counts(tmp_path):
    repo = tmp_path / "wt"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /nonexistent\n")
    res = _run(BULK_COMMIT, tmp_path, "discover", "--repo-path", str(repo))
    assert res.returncode == 0, res.stderr
    assert "NO-REPOS-FOUND" not in res.stdout
    res = _run(CLEAN_GONE, tmp_path, "--repo-path", str(repo))
    assert res.returncode == 0, res.stderr
    assert "1 nothing to clean" in res.stdout


@pytest.mark.parametrize("script", [BULK_COMMIT, CLEAN_GONE])
def test_refuses_bad_repo_paths(script, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    repo = _repo(tmp_path / "repo")
    mode = ["discover"] if script == BULK_COMMIT else []
    bad = [
        ["--repo-path", str(plain)],
        ["--repo-path", "//"],
        ["--repo-path", "/"],
        ["--repo-path", "repo"],
        ["--repo-path", str(tmp_path / "missing")],
        ["--repo-path", f"{repo}/../repo"],
        ["--repo-path", str(repo), "--root", str(tmp_path)],
        ["--repo-path", str(repo), "--repo", "repo"],
    ]
    for args in bad:
        res = _run(script, tmp_path, *mode, *args)
        assert res.returncode != 0, args
        assert res.stdout == "", args


def test_plan_on_bad_path_stages_nothing(tmp_path):
    container = tmp_path / "container"
    inner = _repo(container / "inner")
    res = _run(BULK_COMMIT, tmp_path, "plan", "--repo-path", str(container))
    assert res.returncode != 0
    assert _staged(inner) == ""


@pytest.mark.parametrize("mode", ["discover", "plan", "apply"])
def test_empty_or_missing_repo_path_value_refuses_and_stages_nothing(mode, tmp_path):
    repo = _repo(tmp_path / "repo")
    messages = tmp_path / "msgs.tsv"
    messages.write_text("x\tmsg\n")
    extra = ["--messages", str(messages)] if mode == "apply" else []
    for tail in (["--repo-path", ""], ["--repo-path"]):
        res = _run(BULK_COMMIT, repo.parent, mode, *extra, *tail)
        assert res.returncode != 0, tail
        assert res.stdout == "", tail
        assert _staged(repo) == "", tail


def test_clean_gone_empty_or_missing_repo_path_value_refuses(tmp_path):
    _repo(tmp_path / "repo")
    for tail in (["--repo-path", ""], ["--repo-path"]):
        res = _run(CLEAN_GONE, tmp_path, *tail)
        assert res.returncode != 0, tail
        assert res.stdout == "", tail
