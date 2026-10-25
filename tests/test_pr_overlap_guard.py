"""Tests for pr_overlap_guard hook."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

GUARD_PATH = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-git"
    / "hooks"
    / "pr-overlap"
    / "pr_overlap_guard.py"
)
sys.path.insert(0, str(GUARD_PATH.parent.parent.parent))  # noqa: E402


def load_guard_module() -> ModuleType:
    """Load pr_overlap_guard module dynamically with proper type checking."""
    spec = importlib.util.spec_from_file_location("pr_overlap_guard", GUARD_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module spec from {GUARD_PATH}")
    guard_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard_module)
    return guard_module


def run_hook(event: dict) -> dict | None:
    """Run the hook script and return parsed decision, or None if no decision."""
    try:
        result = subprocess.run(
            [sys.executable, str(GUARD_PATH)],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return None

    if result.returncode != 0:
        return None
    if not result.stdout.strip():
        return None

    try:
        return json.loads(result.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return None


def test_non_bash_tool_passes():
    """Non-Bash tools should pass (allow)."""
    event = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "README.md"},
    }
    decision = run_hook(event)
    assert decision is None


def test_non_guarded_bash_command_passes():
    """Bash commands outside guard scope should pass."""
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
    }
    decision = run_hook(event)
    assert decision is None


def test_missing_stdin_passes():
    """Missing stdin should pass gracefully."""
    result = subprocess.run(
        [sys.executable, str(GUARD_PATH)],
        input="",
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert not result.stdout.strip()


def test_malformed_json_passes():
    """Malformed JSON in stdin should pass gracefully."""
    result = subprocess.run(
        [sys.executable, str(GUARD_PATH)],
        input="not valid json",
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert not result.stdout.strip()


def test_guard_detects_git_switch_c_command():
    """Guard regex recognizes git switch -c."""
    guard_module = load_guard_module()
    assert guard_module.is_guarded_command("git switch -c feature/foo")
    assert guard_module.is_guarded_command("git switch -c fix/bar")


def test_guard_detects_git_checkout_b_command():
    """Guard regex recognizes git checkout -b."""
    guard_module = load_guard_module()
    assert guard_module.is_guarded_command("git checkout -b fix/bar")
    assert guard_module.is_guarded_command("git checkout -b feature/test")


def test_guard_detects_gh_pr_create_command():
    """Guard regex recognizes gh pr create."""
    guard_module = load_guard_module()
    assert guard_module.is_guarded_command("gh pr create --title 'My PR'")
    assert guard_module.is_guarded_command("gh pr create")


def test_guard_detects_glab_mr_create_command():
    """Guard regex recognizes glab mr create."""
    guard_module = load_guard_module()
    assert guard_module.is_guarded_command("glab mr create")
    assert guard_module.is_guarded_command("glab mr create --description 'test'")


def test_excluded_files_do_not_trigger_overlap():
    """Overlap on only .lock/.toml files should be allowed."""
    guard_module = load_guard_module()
    files = {"pyproject.toml", "uv.lock", "src/main.py"}
    filtered = guard_module._filter_excluded(files)
    assert filtered == {"src/main.py"}
    assert "pyproject.toml" not in filtered
    assert "uv.lock" not in filtered


def test_decision_structure():
    """Decision structure matches branch_guard.py format."""
    guard_module = load_guard_module()
    decision = guard_module._decision("deny", "test reason")
    assert "hookSpecificOutput" in decision
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert decision["hookSpecificOutput"]["permissionDecisionReason"] == "test reason"
    assert decision["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_guard_behavior_on_missing_tool():
    """When gh/glab missing, guard should pass (allow), never block on tool error."""
    # This is implicit in the hook's error handling: any exception in decide()
    # returns 0 without printing a decision. We verify indirectly by ensuring
    # the script exits 0 even if gh/glab are not installed.
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "git switch -c feature/test"},
        "cwd": "/tmp",  # a path unlikely to be a real repo
    }
    result = subprocess.run(
        [sys.executable, str(GUARD_PATH)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        timeout=5,
    )
    # Even if the repo or tools don't exist, the script should exit 0 (not raise)
    assert result.returncode == 0


def test_witness_pr_71_nested_in_pr_70(tmp_path):
    """Guard refuses PR #71 (subset) when PR #70 (superset) is in-flight.

    Real git repo test: two branches with overlapping commits.
    decide() must traverse _local_branch_files, detect overlap, deny creation.
    """
    guard_module = load_guard_module()

    repo = tmp_path / "test_repo"
    repo.mkdir()

    # Initialize git repo with local identity
    subprocess.run(
        ["git", "init"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.local"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )

    # Rename default branch to master if needed
    subprocess.run(
        ["git", "checkout", "-b", "master"],
        cwd=str(repo),
        capture_output=True,
        check=False,
    )

    # Initial commit on master branch
    (repo / "file_a.py").write_text("a\n")
    (repo / "file_b.py").write_text("b\n")
    (repo / "file_c.py").write_text("c\n")
    (repo / "file_d.py").write_text("d\n")
    subprocess.run(
        ["git", "add", "."],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )

    # Branch 1: PR #70 modifies all 4 files
    subprocess.run(
        ["git", "checkout", "-b", "pr-70"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    (repo / "file_a.py").write_text("a-70\n")
    (repo / "file_b.py").write_text("b-70\n")
    (repo / "file_c.py").write_text("c-70\n")
    (repo / "file_d.py").write_text("d-70\n")
    subprocess.run(
        ["git", "add", "."],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "pr-70"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )

    # Branch 2: PR #71 modifies only 2 files (subset)
    # Checkout master first, then create pr-71 from master (not from pr-70)
    subprocess.run(
        ["git", "checkout", "master"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", "pr-71"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    (repo / "file_a.py").write_text("a-71\n")
    (repo / "file_b.py").write_text("b-71\n")
    subprocess.run(
        ["git", "add", "."],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "pr-71"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )

    # Call decide() with gh pr create event
    # gh won't run in test repo (no remote), but _local_branch_files will see pr-70
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "gh pr create --base master --head pr-71 --title t"},
        "cwd": str(repo),
    }

    decision = guard_module.decide(event, repo)

    assert decision is not None, "Guard should deny overlapping PR creation"
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"].lower()
    assert "overlap" in reason or "already in flight" in reason
    assert "file_a.py" in reason or "file_b.py" in reason


def _init_repo(repo: Path) -> None:
    """Create a git repo with test identity and a main branch."""
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=str(repo), capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.local"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )


def _commit_all(repo: Path, message: str) -> None:
    """Stage and commit every change in the repo."""
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )


def test_stale_local_main_behind_upstream_not_in_flight(tmp_path):
    """A local main behind origin/main after a remote merge is never in flight.

    Witness of the stale-main false positive: PR merged remotely (origin/main
    advances), local main left behind, working tree touches the merged file. The
    two-tip diff origin/main..main is non-empty (the merged files, reversed), so
    main used to be counted as carrying them and every checkout -b on those files
    was denied.
    """
    guard_module = load_guard_module()

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    (repo / "file_m.py").write_text("base\n")
    (repo / "other.py").write_text("base\n")
    _commit_all(repo, "init")

    subprocess.run(
        ["git", "checkout", "-b", "feature/pr1"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    (repo / "file_m.py").write_text("merged\n")
    _commit_all(repo, "change file_m")

    # Remote merge: origin/main advances past local main, which stays behind.
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "refs/heads/feature/pr1"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "origin.git")],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "branch", "--set-upstream-to=origin/main", "main"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )

    # Session sits on the merged branch with uncommitted work on the merged file.
    subprocess.run(
        ["git", "checkout", "feature/pr1"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    (repo / "file_m.py").write_text("wip\n")

    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "git checkout -b fix/next"},
        "cwd": str(repo),
    }
    decision = guard_module.decide(event, repo)
    assert decision is None, (
        "A branch whose tip is an ancestor of its base carries nothing — "
        f"decision should be allow, got {decision}"
    )


def test_squash_merged_branch_stays_in_flight(tmp_path):
    """A squash-merged branch (distinct tip, broken ancestry) stays in flight.

    The base receives the branch's file content through an equivalent commit
    instead of a merge, so the branch tip is not an ancestor of the base and
    the ancestry check does not skip it. Once the base moves on past the
    squash, the two-tip diff is non-empty again and a checkout -b touching
    the file is denied — an accepted limitation.
    """
    guard_module = load_guard_module()

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    (repo / "file_s.py").write_text("base\n")
    _commit_all(repo, "init")

    subprocess.run(
        ["git", "checkout", "-b", "feature/pr2"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    (repo / "file_s.py").write_text("squashed\n")
    _commit_all(repo, "change file_s")

    # Squash : la base reçoit le même contenu par son propre commit, sans merge.
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    (repo / "file_s.py").write_text("squashed\n")
    _commit_all(repo, "squash-merge equivalent")
    # La base avance après le squash — le diff entre tips redevient non vide.
    (repo / "file_s.py").write_text("squashed\nfollow-up\n")
    _commit_all(repo, "follow-up on main")

    # Nouveau travail sur le même fichier, non commité, depuis le tip de la base.
    (repo / "file_s.py").write_text("wip\n")

    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "git checkout -b fix/next"},
        "cwd": str(repo),
    }
    decision = guard_module.decide(event, repo)
    assert decision is not None, (
        "A squash-merged branch keeps a distinct tip — it stays counted as "
        "in flight and the checkout must be denied"
    )
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "file_s.py" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_is_ancestor_nonexistent_base_returns_false(tmp_path):
    """A base that does not exist is not an ancestor — the branch stays a candidate.

    git exits non-zero on an unknown ref, so _is_ancestor returns False and
    the caller keeps treating the branch as potentially in flight, exactly
    as on any other git error.
    """
    guard_module = load_guard_module()

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "file.py").write_text("base\n")
    _commit_all(repo, "init")

    assert guard_module._is_ancestor(repo, "main", "no-such-base") is False
    # Contrôle : la réponse est True quand l'ancestrité tient.
    assert guard_module._is_ancestor(repo, "main", "main") is True
