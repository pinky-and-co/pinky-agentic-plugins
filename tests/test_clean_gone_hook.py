#!/usr/bin/env python3
"""Tests for clean-gone hook — end-to-end integration with bulk_clean_gone.sh.

Tests verify the hook traverses successfully and triggers cleanup via the bash script.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def git(repo: Path, *args: str) -> str:
    """Run a git command in repo; raise on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def create_test_repo(path: Path, bare: bool = False) -> Path:
    """Create a test git repository."""
    if bare:
        subprocess.run(
            ["git", "init", "--bare", str(path)],
            capture_output=True,
            check=True,
        )
    else:
        subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
        git(path, "config", "user.email", "test@example.com")
        git(path, "config", "user.name", "Test User")
    return path


def commit_file(repo: Path, filename: str, content: str = "test") -> str:
    """Create, stage, and commit a file in a repo."""
    file_path = repo / filename
    file_path.write_text(content)
    git(repo, "add", filename)
    git(repo, "commit", "-m", f"Add {filename}")
    return git(repo, "rev-parse", "HEAD")


class TestCleanGoneHook:
    """Tests that traverse the hook script itself."""

    def test_hook_script_executes_without_error(self) -> None:
        """Hook executes successfully on a workspace with no gone branches."""
        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            local_repo = tmpdir_path / "test-repo"
            remote_repo = tmpdir_path / "remote"

            local_repo.mkdir()
            create_test_repo(remote_repo, bare=True)

            subprocess.run(
                ["git", "clone", str(remote_repo), str(local_repo)],
                capture_output=True,
                check=True,
            )

            commit_file(local_repo, "file1.txt")
            git(local_repo, "push", "-u", "origin", "main")

            # Execute hook script on workspace with no cleanup needed
            hook_script = (
                Path(__file__).parent.parent
                / "plugins"
                / "pc-git"
                / "hooks"
                / "clean-gone"
                / "clean_gone_hook.py"
            )
            event = {"cwd": str(tmpdir_path)}

            result = subprocess.run(
                [sys.executable, str(hook_script)],
                input=json.dumps(event),
                cwd=tmpdir_path,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

            # Hook must always succeed (exit 0)
            assert result.returncode == 0

    def test_hook_cleans_gone_branches_in_workspace(self) -> None:
        """Hook actually cleans [gone] branches when they exist."""
        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            local_repo = tmpdir_path / "test-repo"
            remote_repo = tmpdir_path / "remote"

            local_repo.mkdir()
            create_test_repo(remote_repo, bare=True)

            subprocess.run(
                ["git", "clone", str(remote_repo), str(local_repo)],
                capture_output=True,
                check=True,
            )

            # Create a branch that will be [gone]
            commit_file(local_repo, "file1.txt")
            git(local_repo, "push", "-u", "origin", "main")
            git(local_repo, "checkout", "-b", "feature/gone-branch")
            commit_file(local_repo, "file2.txt")
            git(local_repo, "push", "-u", "origin", "feature/gone-branch")

            # Delete the remote branch
            git(remote_repo, "update-ref", "-d", "refs/heads/feature/gone-branch")
            git(local_repo, "fetch", "--prune", "origin")

            # Verify branch exists before hook
            branches_before = git(local_repo, "branch").split("\n")
            assert any("feature/gone-branch" in b for b in branches_before)

            # Execute hook
            hook_script = (
                Path(__file__).parent.parent
                / "plugins"
                / "pc-git"
                / "hooks"
                / "clean-gone"
                / "clean_gone_hook.py"
            )
            event = {"cwd": str(tmpdir_path)}

            result = subprocess.run(
                [sys.executable, str(hook_script)],
                input=json.dumps(event),
                cwd=tmpdir_path,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

            # Hook must succeed
            assert result.returncode == 0

            # Verify branch was deleted
            branches_after = git(local_repo, "branch").split("\n")
            assert not any("feature/gone-branch" in b for b in branches_after)

    def test_hook_preserves_live_branches(self) -> None:
        """Hook does NOT delete branches with live remotes."""
        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            local_repo = tmpdir_path / "test-repo"
            remote_repo = tmpdir_path / "remote"

            local_repo.mkdir()
            create_test_repo(remote_repo, bare=True)

            subprocess.run(
                ["git", "clone", str(remote_repo), str(local_repo)],
                capture_output=True,
                check=True,
            )

            # Create a branch with live remote
            commit_file(local_repo, "file1.txt")
            git(local_repo, "push", "-u", "origin", "main")
            git(local_repo, "checkout", "-b", "feature/live")
            commit_file(local_repo, "file2.txt")
            git(local_repo, "push", "-u", "origin", "feature/live")

            # Execute hook (remote NOT deleted)
            hook_script = (
                Path(__file__).parent.parent
                / "plugins"
                / "pc-git"
                / "hooks"
                / "clean-gone"
                / "clean_gone_hook.py"
            )
            event = {"cwd": str(tmpdir_path)}

            result = subprocess.run(
                [sys.executable, str(hook_script)],
                input=json.dumps(event),
                cwd=tmpdir_path,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

            assert result.returncode == 0

            # Verify live branch still exists
            branches_after = git(local_repo, "branch").split("\n")
            assert any("feature/live" in b for b in branches_after)

    def test_hook_preserves_local_only_branches(self) -> None:
        """Hook does NOT delete branches with no remote tracking."""
        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            local_repo = tmpdir_path / "test-repo"
            remote_repo = tmpdir_path / "remote"

            local_repo.mkdir()
            create_test_repo(remote_repo, bare=True)

            subprocess.run(
                ["git", "clone", str(remote_repo), str(local_repo)],
                capture_output=True,
                check=True,
            )

            # Create a local-only branch (never pushed)
            commit_file(local_repo, "file1.txt")
            git(local_repo, "push", "-u", "origin", "main")
            git(local_repo, "checkout", "-b", "local-feature")
            commit_file(local_repo, "file2.txt")
            # DO NOT push this branch

            # Execute hook
            hook_script = (
                Path(__file__).parent.parent
                / "plugins"
                / "pc-git"
                / "hooks"
                / "clean-gone"
                / "clean_gone_hook.py"
            )
            event = {"cwd": str(tmpdir_path)}

            result = subprocess.run(
                [sys.executable, str(hook_script)],
                input=json.dumps(event),
                cwd=tmpdir_path,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

            assert result.returncode == 0

            # Verify local-only branch still exists
            branches_after = git(local_repo, "branch").split("\n")
            assert any("local-feature" in b for b in branches_after)

    def test_hook_handles_repo_without_remote(self) -> None:
        """Hook does not error on repos without any remote."""
        with TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            local_repo = tmpdir_path / "test-repo"

            local_repo.mkdir()
            create_test_repo(local_repo)
            commit_file(local_repo, "file1.txt")

            # Execute hook on repo with no remote
            hook_script = (
                Path(__file__).parent.parent
                / "plugins"
                / "pc-git"
                / "hooks"
                / "clean-gone"
                / "clean_gone_hook.py"
            )
            event = {"cwd": str(tmpdir_path)}

            result = subprocess.run(
                [sys.executable, str(hook_script)],
                input=json.dumps(event),
                cwd=tmpdir_path,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

            # Hook must succeed (never fail)
            assert result.returncode == 0
