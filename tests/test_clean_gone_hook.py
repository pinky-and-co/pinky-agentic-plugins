#!/usr/bin/env python3
"""Tests for clean-gone hook — end-to-end integration with bulk_clean_gone.sh.

Tests verify the hook traverses successfully and triggers cleanup via the bash script.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

PLUGIN_DIR = Path(__file__).parent.parent / "plugins" / "pc-git"
HOOK_SCRIPT = PLUGIN_DIR / "hooks" / "clean-gone" / "clean_gone_hook.py"

_ISOLATION_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "CLAUDE_PROJECT_DIR",
)


def _isolated_env() -> dict[str, str]:
    """The current environment minus every variable that would point a subprocess elsewhere.

    The `GIT_*` variables redirect git's repo discovery regardless of cwd, and an outer git
    process (e.g. the commit invoking this suite) exports them. `CLAUDE_PROJECT_DIR` outranks
    the event's cwd in the hook, so one inherited from a live session would aim it at the real
    repo.
    """
    return {k: v for k, v in os.environ.items() if k not in _ISOLATION_VARS}


def git(repo: Path, *args: str) -> str:
    """Run a git command in repo; raise on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=_isolated_env(),
    )
    return result.stdout.strip()


def create_test_repo(path: Path, bare: bool = False) -> Path:
    """Create a test git repository."""
    if bare:
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(path)],
            capture_output=True,
            check=True,
            env=_isolated_env(),
        )
    else:
        subprocess.run(
            ["git", "init", "-b", "main", str(path)],
            capture_output=True,
            check=True,
            env=_isolated_env(),
        )
        git(path, "config", "user.email", "test@example.com")
        git(path, "config", "user.name", "Test User")
    return path


def clone(remote: Path, dest: Path) -> Path:
    """Clone `remote` into `dest` with a test identity configured."""
    subprocess.run(
        ["git", "clone", str(remote), str(dest)],
        capture_output=True,
        check=True,
        env=_isolated_env(),
    )
    git(dest, "config", "user.email", "test@example.com")
    git(dest, "config", "user.name", "Test User")
    return dest


def commit_file(repo: Path, filename: str, content: str = "test") -> str:
    """Create, stage, and commit a file in a repo."""
    file_path = repo / filename
    file_path.write_text(content)
    git(repo, "add", filename)
    git(repo, "commit", "-m", f"Add {filename}")
    return git(repo, "rev-parse", "HEAD")


def branches(repo: Path) -> list[str]:
    """Local branch names of `repo`."""
    return git(repo, "branch", "--format=%(refname:short)").split("\n")


def run_hook(root: Path) -> subprocess.CompletedProcess[str]:
    """Run the SessionStart hook with `root` as the session's working directory."""
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps({"cwd": str(root)}),
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env=_isolated_env(),
    )


def system_message(result: subprocess.CompletedProcess[str]) -> str:
    """The `systemMessage` the hook emitted, or "" when it printed nothing."""
    if not result.stdout.strip():
        return ""
    return json.loads(result.stdout)["systemMessage"]


@pytest.fixture
def workspace() -> Iterator[tuple[Path, Path, Path]]:
    """A bare remote and a clone with `main` pushed: `(tmpdir, remote, local)`."""
    with TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        remote = create_test_repo(tmpdir_path / "remote", bare=True)
        local = clone(remote, tmpdir_path / "test-repo")
        commit_file(local, "file1.txt")
        git(local, "push", "-u", "origin", "main")
        yield tmpdir_path, remote, local


class TestCleanGoneHook:
    """Tests that traverse the hook script itself."""

    def test_registered_on_session_start(self) -> None:
        """The hook runs at SessionStart — a PR merges after the session that opened it ends."""
        hooks = json.loads((PLUGIN_DIR / "hooks" / "hooks.json").read_text())["hooks"]
        commands = [
            h["command"]
            for group in hooks.get("SessionStart", [])
            for h in group["hooks"]
        ]
        assert any("clean_gone_hook.py" in c for c in commands)
        assert "SessionEnd" not in hooks

    def test_hook_silent_when_nothing_to_do(
        self, workspace: tuple[Path, Path, Path]
    ) -> None:
        """Hook succeeds and prints nothing on an up-to-date repo with no gone branch."""
        _, _, local = workspace

        result = run_hook(local)

        assert result.returncode == 0
        assert result.stdout == ""

    def test_hook_cleans_gone_branch_and_reports_it(
        self, workspace: tuple[Path, Path, Path]
    ) -> None:
        """Hook deletes a [gone] branch, moves HEAD off it, and names both in its message."""
        _, remote, local = workspace
        git(local, "checkout", "-b", "feature/gone-branch")
        commit_file(local, "file2.txt")
        git(local, "push", "-u", "origin", "feature/gone-branch")
        git(remote, "update-ref", "-d", "refs/heads/feature/gone-branch")

        result = run_hook(local)

        assert result.returncode == 0
        assert branches(local) == ["main"]
        assert system_message(result) == (
            "clean-gone:\n"
            "  ✅ test-repo                    "
            "HEAD feature/gone-branch → main · removed: feature/gone-branch"
        )

    def test_hook_removes_worktree_for_gone_branch(
        self, workspace: tuple[Path, Path, Path]
    ) -> None:
        """Hook removes the worktree registered for a [gone] branch, along with the branch."""
        tmpdir, remote, local = workspace
        worktree_path = tmpdir / "gone-worktree"
        git(local, "checkout", "-b", "feature/gone-worktree")
        commit_file(local, "file2.txt")
        git(local, "push", "-u", "origin", "feature/gone-worktree")
        git(local, "checkout", "main")
        git(local, "worktree", "add", str(worktree_path), "feature/gone-worktree")
        git(remote, "update-ref", "-d", "refs/heads/feature/gone-worktree")

        result = run_hook(local)

        assert result.returncode == 0
        assert not worktree_path.exists()
        assert str(worktree_path) not in git(local, "worktree", "list")
        assert "feature/gone-worktree" not in branches(local)

    def test_hook_cleans_merged_branch_pushed_without_upstream(
        self, workspace: tuple[Path, Path, Path]
    ) -> None:
        """Hook deletes a merged branch whose remote is gone even though it has no upstream.

        `git push origin <branch>` without `-u` sets no upstream, so `branch -vv` never shows
        `[origin/x: gone]` for it.
        """
        _, remote, local = workspace
        git(local, "checkout", "-b", "feature/no-upstream")
        commit_file(local, "file2.txt")
        git(local, "push", "origin", "feature/no-upstream")
        # Merge côté serveur, puis suppression de la branche distante (delete_branch_on_merge).
        git(local, "checkout", "main")
        git(local, "merge", "--no-ff", "-m", "merge", "feature/no-upstream")
        git(local, "push", "origin", "main")
        git(remote, "update-ref", "-d", "refs/heads/feature/no-upstream")

        result = run_hook(local)

        assert result.returncode == 0
        assert "feature/no-upstream" not in branches(local)

    def test_hook_preserves_fresh_branch_without_commits(
        self, workspace: tuple[Path, Path, Path]
    ) -> None:
        """Hook does NOT delete a just-created branch — its tip is on main, but no PR merged it."""
        _, _, local = workspace
        git(local, "checkout", "-b", "feature/fresh")

        result = run_hook(local)

        assert result.returncode == 0
        assert "feature/fresh" in branches(local)

    def test_hook_preserves_live_branches(
        self, workspace: tuple[Path, Path, Path]
    ) -> None:
        """Hook does NOT delete branches with live remotes."""
        _, _, local = workspace
        git(local, "checkout", "-b", "feature/live")
        commit_file(local, "file2.txt")
        git(local, "push", "-u", "origin", "feature/live")

        result = run_hook(local)

        assert result.returncode == 0
        assert "feature/live" in branches(local)

    def test_hook_preserves_local_only_branches(
        self, workspace: tuple[Path, Path, Path]
    ) -> None:
        """Hook does NOT delete branches with no remote tracking."""
        _, _, local = workspace
        git(local, "checkout", "-b", "local-feature")
        commit_file(local, "file2.txt")

        result = run_hook(local)

        assert result.returncode == 0
        assert "local-feature" in branches(local)

    def test_hook_keeps_head_on_live_branch_while_cleaning_another(
        self, workspace: tuple[Path, Path, Path]
    ) -> None:
        """A [gone] branch elsewhere never switches HEAD off the live branch being worked on."""
        _, remote, local = workspace
        git(local, "checkout", "-b", "feature/gone")
        commit_file(local, "file2.txt")
        git(local, "push", "-u", "origin", "feature/gone")
        git(local, "checkout", "main")
        git(local, "checkout", "-b", "feature/live")
        commit_file(local, "file3.txt")
        git(local, "push", "-u", "origin", "feature/live")
        git(remote, "update-ref", "-d", "refs/heads/feature/gone")

        result = run_hook(local)

        assert result.returncode == 0
        assert git(local, "branch", "--show-current") == "feature/live"
        assert "feature/gone" not in branches(local)

    def test_hook_fast_forwards_base_and_reports_it(
        self, workspace: tuple[Path, Path, Path]
    ) -> None:
        """Hook brings the checked-out base up to date with its remote, and says by how much."""
        tmpdir, remote, local = workspace
        other = clone(remote, tmpdir / "other-clone")
        commit_file(other, "upstream.txt")
        git(other, "push", "origin", "main")

        result = run_hook(local)

        assert result.returncode == 0
        assert git(local, "rev-parse", "main") == git(other, "rev-parse", "main")
        assert system_message(result) == (
            "clean-gone:\n  ✅ test-repo                    main +1 commit(s)"
        )

    def test_hook_fast_forwards_base_without_leaving_live_branch(
        self, workspace: tuple[Path, Path, Path]
    ) -> None:
        """The base is updated even while HEAD sits on a live feature branch, which stays put."""
        tmpdir, remote, local = workspace
        git(local, "checkout", "-b", "feature/live")
        other = clone(remote, tmpdir / "other-clone")
        commit_file(other, "upstream.txt")
        git(other, "push", "origin", "main")

        result = run_hook(local)

        assert result.returncode == 0
        assert git(local, "branch", "--show-current") == "feature/live"
        assert git(local, "rev-parse", "main") == git(other, "rev-parse", "main")

    def test_hook_skips_a_root_that_is_not_a_repo(
        self, workspace: tuple[Path, Path, Path]
    ) -> None:
        """A session opened at a workspace root (not itself a repo) sweeps nothing."""
        tmpdir, remote, local = workspace
        git(local, "checkout", "-b", "feature/gone-branch")
        commit_file(local, "file2.txt")
        git(local, "push", "-u", "origin", "feature/gone-branch")
        git(local, "checkout", "main")
        git(remote, "update-ref", "-d", "refs/heads/feature/gone-branch")

        result = run_hook(tmpdir)

        assert result.returncode == 0
        assert result.stdout == ""
        assert "feature/gone-branch" in branches(local)

    def test_hook_silent_on_repo_without_remote(self) -> None:
        """Hook neither errors nor reports on a repo with no remote."""
        with TemporaryDirectory() as tmpdir:
            local_repo = create_test_repo(Path(tmpdir) / "test-repo")
            commit_file(local_repo, "file1.txt")

            result = run_hook(local_repo)

            assert result.returncode == 0
            assert result.stdout == ""
