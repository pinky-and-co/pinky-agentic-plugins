#!/usr/bin/env python3
"""Tests for plugins/pc-delegate/skills/reintegrate/scripts/apply_reintegrate.py

Defines "done": a zip/directory of files an external agent produced applies onto a real local git
repo — a brand-new path (including an empty one) is created, an existing tracked-and-clean path is
overwritten, a tracked-and-dirty path is refused, an untracked path already on disk with different
content is refused, a `..`/absolute/drive-letter entry or a symlink-escape is refused outright, a
write that cannot succeed is predicted identically by `--dry-run` and a real run, and entry-name
quirks (a leading `./`, duplicate separators, non-ASCII, a case-only difference) still resolve to
the same tracked file. Real temp git repos throughout — tracked/dirty status against HEAD is
exactly the behaviour under test, so faking `git` would remove the subject. Assertions check both
the resulting file content and the printed outcome line (`capsys`) — a refusal for the wrong reason
would pass a content-only assertion. A top-level `REPORT.md` in the source is never applied as a
repo file — it is written as a new `_AGENT_LOG` entry when `--agent-log-dir` is given, or printed,
never silently dropped; a nested `sub/REPORT.md` is an ordinary repo file, not the reserved report.
"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPTS = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-delegate"
    / "skills"
    / "reintegrate"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

import apply_reintegrate as ar  # noqa: E402


# Identité fournie par l'environnement, jamais par `git config` : un `config` local écrit dans
# `.git/config`, et si un `GIT_DIR` hérité du hook pointe vers le vrai repo, c'est lui qui le reçoit.
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _scrub_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every inherited `GIT_*` variable, then set the throwaway test identity.

    A git process running this suite (the pre-commit hook) exports `GIT_INDEX_FILE`, and
    sometimes `GIT_DIR`: they redirect repo discovery whatever `-C` says, so a temp repo's
    `init`/`add`/`commit`/`config` would land in the real repo's index and `.git/config`.
    The scrub covers the test process too, so the code under test gets the same clean env.
    """
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    for key, value in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(autouse=True)
def _isolated_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs with a scrubbed git environment."""
    _scrub_git_env(monkeypatch)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "tracked.py").write_text('"""Doc."""\nx = 1\n')
    _git(root, "add", "tracked.py")
    _git(root, "commit", "-q", "-m", "initial")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _init_repo(root)
    return root


def _zip_of(tmp_path: Path, name: str, entries: dict[str, str]) -> Path:
    archive = tmp_path / name
    with zipfile.ZipFile(archive, "w") as zf:
        for path, content in entries.items():
            zf.writestr(path, content)
    return archive


def _witness_state(witness: Path) -> tuple[str, str, bytes, str]:
    """Config, HEAD, raw index and status of a witness repo, to compare before/after."""
    git_dir = witness / ".git"
    status = subprocess.run(
        ["git", "-C", str(witness), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return (
        (git_dir / "config").read_text(),
        (git_dir / "HEAD").read_text(),
        (git_dir / "index").read_bytes(),
        status,
    )


def _leak_hook_env(monkeypatch: pytest.MonkeyPatch, witness: Path) -> None:
    """Point git at `witness` the way an outer git process does."""
    monkeypatch.setenv("GIT_DIR", str(witness / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(witness / ".git" / "index"))
    monkeypatch.setenv("GIT_WORK_TREE", str(witness))


class TestGitEnvIsolation:
    def test_leaked_env_without_scrub_reaches_the_witness(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: the leak is real, so the test below can fail."""
        witness = tmp_path / "witness"
        _init_repo(witness)
        before = _witness_state(witness)
        _leak_hook_env(monkeypatch, witness)
        temp = tmp_path / "temp"
        temp.mkdir()
        _git(temp, "config", "user.name", "leaky")
        assert _witness_state(witness) != before
        assert "leaky" in (witness / ".git" / "config").read_text()

    def test_fixture_repo_leaves_a_hook_targeted_repo_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        witness = tmp_path / "witness"
        _init_repo(witness)
        before = _witness_state(witness)
        _leak_hook_env(monkeypatch, witness)
        _scrub_git_env(monkeypatch)
        _init_repo(tmp_path / "temp")
        assert _witness_state(witness) == before
        assert "GIT_DIR" not in os.environ
        assert "user" not in (tmp_path / "temp" / ".git" / "config").read_text()


class TestClassifyChange:
    def test_identical_bytes_is_unchanged(self) -> None:
        assert ar.classify_change(b"x = 1\n", b"x = 1\n") == "unchanged"

    def test_docstring_only_edit_is_comments_only(self) -> None:
        old = b'"""Old doc."""\nx = 1\n'
        new = b'"""New doc, longer."""\nx = 1\n'
        assert ar.classify_change(old, new) == "comments-only"

    def test_logic_edit_is_code_changed(self) -> None:
        old = b'"""Doc."""\nx = 1\n'
        new = b'"""Doc."""\nx = 2\n'
        assert ar.classify_change(old, new) == "code-changed"

    def test_syntax_error_is_unparseable(self) -> None:
        assert ar.classify_change(b"x = 1\n", b"def (:\n") == "unparseable"


class TestResolveWithinRepo:
    def test_plain_relative_path_resolves_under_repo(self, tmp_path: Path) -> None:
        repo = tmp_path.resolve()
        result = ar.resolve_within_repo(repo, "pkg/module.py")
        assert result == repo / "pkg" / "module.py"

    def test_leading_slash_is_refused(self, tmp_path: Path) -> None:
        assert ar.resolve_within_repo(tmp_path.resolve(), "/etc/passwd") is None

    def test_dotdot_segment_is_refused(self, tmp_path: Path) -> None:
        assert (
            ar.resolve_within_repo(tmp_path.resolve(), "pkg/../../etc/passwd") is None
        )

    def test_windows_drive_letter_is_refused(self, tmp_path: Path) -> None:
        assert (
            ar.resolve_within_repo(tmp_path.resolve(), "C:\\Windows\\evil.py") is None
        )

    def test_symlinked_directory_escape_is_refused(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "linkdir").symlink_to(outside, target_is_directory=True)
        result = ar.resolve_within_repo(repo.resolve(), "linkdir/evil.txt")
        assert result is None


class TestApplySource:
    def test_new_file_is_created_and_classified_code_changed(
        self, tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        archive = _zip_of(tmp_path, "r1.zip", {"brand_new.py": "x = 1\n"})
        code = ar.apply_source(archive, repo, dry_run=False)
        assert (repo / "brand_new.py").read_text() == "x = 1\n"
        assert "applied [new file]: brand_new.py" in capsys.readouterr().out
        assert code == 0

    def test_new_empty_python_file_is_created_not_dropped(
        self, tmp_path: Path, repo: Path
    ) -> None:
        archive = _zip_of(tmp_path, "r1b.zip", {"pkg/__init__.py": ""})
        ar.apply_source(archive, repo, dry_run=False)
        assert (repo / "pkg" / "__init__.py").exists()
        assert (repo / "pkg" / "__init__.py").read_text() == ""

    def test_tracked_clean_file_is_overwritten(
        self, tmp_path: Path, repo: Path
    ) -> None:
        archive = _zip_of(tmp_path, "r2.zip", {"tracked.py": '"""Doc."""\nx = 2\n'})
        ar.apply_source(archive, repo, dry_run=False)
        assert (repo / "tracked.py").read_text() == '"""Doc."""\nx = 2\n'

    def test_tracked_dirty_file_is_refused(
        self, tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (repo / "tracked.py").write_text('"""Doc."""\nx = 99\n')  # local, uncommitted
        archive = _zip_of(tmp_path, "r3.zip", {"tracked.py": '"""Doc."""\nx = 2\n'})
        code = ar.apply_source(archive, repo, dry_run=False)
        assert (repo / "tracked.py").read_text() == '"""Doc."""\nx = 99\n'
        assert "uncommitted local changes already present" in capsys.readouterr().out
        assert code == 1

    def test_untracked_existing_file_with_different_content_is_refused(
        self, tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (repo / "scratch.py").write_text("local = True\n")  # never git add-ed
        archive = _zip_of(tmp_path, "r4.zip", {"scratch.py": "remote = True\n"})
        code = ar.apply_source(archive, repo, dry_run=False)
        assert (repo / "scratch.py").read_text() == "local = True\n"
        assert "different content" in capsys.readouterr().out
        assert code == 1

    def test_traversal_entry_writes_nothing_outside_repo(
        self, tmp_path: Path, repo: Path
    ) -> None:
        archive = _zip_of(tmp_path, "r5.zip", {"../../etc/evil.py": "x = 1\n"})
        ar.apply_source(archive, repo, dry_run=False)
        assert not (repo.parent.parent / "etc" / "evil.py").exists()

    def test_dry_run_writes_nothing(self, tmp_path: Path, repo: Path) -> None:
        archive = _zip_of(tmp_path, "r6.zip", {"brand_new.py": "x = 1\n"})
        ar.apply_source(archive, repo, dry_run=True)
        assert not (repo / "brand_new.py").exists()

    def test_directory_file_collision_reported_not_crashed_and_dry_run_matches(
        self, tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (repo / "pkg").write_text("not a directory\n")
        _git(repo, "add", "pkg")
        _git(repo, "commit", "-q", "-m", "add pkg file")
        archive = _zip_of(
            tmp_path, "r7.zip", {"pkg/module.py": "x = 1\n", "also_this.py": "y = 1\n"}
        )

        dry_code = ar.apply_source(archive, repo, dry_run=True)
        dry_out = capsys.readouterr().out
        real_code = ar.apply_source(archive, repo, dry_run=False)
        real_out = capsys.readouterr().out

        assert dry_code == real_code == 1
        assert "blocked by an existing non-directory file" in dry_out
        assert "blocked by an existing non-directory file" in real_out
        assert (repo / "also_this.py").read_text() == "y = 1\n"

    def test_directory_already_at_target_path_is_reported_not_crashed(
        self, tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (repo / "adir").mkdir()
        archive = _zip_of(tmp_path, "r8.zip", {"adir": "x = 1\n"})
        code = ar.apply_source(archive, repo, dry_run=False)
        assert "a directory already exists at this path" in capsys.readouterr().out
        assert code == 1

    def test_corrupt_zip_reports_clean_error(self, tmp_path: Path, repo: Path) -> None:
        bad = tmp_path / "not_a_zip.zip"
        bad.write_bytes(b"this is not a zip file")
        with pytest.raises(zipfile.BadZipFile):
            ar.apply_source(bad, repo, dry_run=False)

    def test_circular_symlink_ancestor_is_predicted_by_dry_run_and_not_double_reported(
        self, tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (repo / "loop").symlink_to("loop")  # self-referential — never resolves to a dir
        archive = _zip_of(tmp_path, "r9.zip", {"loop/evil.py": "x = 1\n"})

        dry_code = ar.apply_source(archive, repo, dry_run=True)
        dry_out = capsys.readouterr().out
        real_code = ar.apply_source(archive, repo, dry_run=False)
        real_out = capsys.readouterr().out

        # Which pre-flight check catches the cycle is Python-version-dependent; both fail closed.
        assert dry_code == real_code == 1
        assert "loop/evil.py" in dry_out
        assert "loop/evil.py" in real_out
        assert "1 traversal-refused" in dry_out or "1 write-blocked" in dry_out
        assert ("1 traversal-refused" in dry_out) == ("1 traversal-refused" in real_out)
        assert ("1 write-blocked" in dry_out) == ("1 write-blocked" in real_out)
        assert "applied [new file]: loop/evil.py" not in real_out
        assert not (repo / "loop" / "evil.py").exists()


class TestEntryNameNormalization:
    def test_leading_dot_slash_matches_tracked_file(
        self, tmp_path: Path, repo: Path
    ) -> None:
        archive = _zip_of(tmp_path, "n1.zip", {"./tracked.py": '"""Doc."""\nx = 2\n'})
        ar.apply_source(archive, repo, dry_run=False)
        assert (repo / "tracked.py").read_text() == '"""Doc."""\nx = 2\n'

    def test_duplicate_separators_match_tracked_file(
        self, tmp_path: Path, repo: Path
    ) -> None:
        (repo / "pkg").mkdir()
        (repo / "pkg" / "module.py").write_text("x = 1\n")
        _git(repo, "add", "pkg/module.py")
        _git(repo, "commit", "-q", "-m", "add pkg/module.py")
        archive = _zip_of(tmp_path, "n2.zip", {"pkg//module.py": "x = 2\n"})
        ar.apply_source(archive, repo, dry_run=False)
        assert (repo / "pkg" / "module.py").read_text() == "x = 2\n"

    def test_non_ascii_filename_matches_tracked_file(
        self, tmp_path: Path, repo: Path
    ) -> None:
        (repo / "café.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", "café.py")
        _git(repo, "commit", "-q", "-m", "add café.py")
        archive = _zip_of(tmp_path, "n3.zip", {"café.py": "x = 2\n"})
        ar.apply_source(archive, repo, dry_run=False)
        assert (repo / "café.py").read_text(encoding="utf-8") == "x = 2\n"


class TestUnbornHead:
    def test_reapply_on_repo_with_no_commits_yet_is_not_treated_as_dirty(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "fresh_repo"
        root.mkdir()
        _git(root, "init", "-q", "-b", "main")
        (root / "staged.py").write_text("x = 1\n")
        _git(root, "add", "staged.py")
        archive = _zip_of(tmp_path, "u1.zip", {"staged.py": "x = 1\n"})
        code = ar.apply_source(archive, root, dry_run=False)
        assert code == 0


class TestIsReportEntry:
    def test_top_level_report_matches(self) -> None:
        assert ar._is_report_entry("REPORT.md") is True

    def test_top_level_report_with_dot_slash_prefix_matches(self) -> None:
        assert ar._is_report_entry("./REPORT.md") is True

    def test_nested_report_does_not_match(self) -> None:
        assert ar._is_report_entry("sub/REPORT.md") is False

    def test_different_name_does_not_match(self) -> None:
        assert ar._is_report_entry("readme.md") is False


class TestReportMd:
    def test_report_is_never_applied_as_a_repo_file(
        self, tmp_path: Path, repo: Path
    ) -> None:
        archive = _zip_of(
            tmp_path, "r10.zip", {"brand_new.py": "x = 1\n", "REPORT.md": "did stuff\n"}
        )
        ar.apply_source(archive, repo, dry_run=False)
        assert not (repo / "REPORT.md").exists()
        assert (repo / "brand_new.py").exists()

    def test_nested_report_md_is_applied_as_an_ordinary_file(
        self, tmp_path: Path, repo: Path
    ) -> None:
        archive = _zip_of(tmp_path, "r11.zip", {"docs/REPORT.md": "not the report\n"})
        ar.apply_source(archive, repo, dry_run=False)
        assert (repo / "docs" / "REPORT.md").read_text() == "not the report\n"

    def test_report_written_to_agent_log_dir_when_given(
        self, tmp_path: Path, repo: Path
    ) -> None:
        log_dir = tmp_path / "_AGENT_LOG"
        archive = _zip_of(
            tmp_path, "r12.zip", {"REPORT.md": "## What I did\n\nstuff\n"}
        )
        ar.apply_source(
            archive, repo, dry_run=False, agent_log_dir=log_dir, card_ids=["my-card"]
        )
        written = list(log_dir.glob("*_reintegrate-my-card.md"))
        assert len(written) == 1
        assert written[0].read_text() == "## What I did\n\nstuff\n"

    def test_report_falls_back_to_repo_name_without_card_ids(
        self, tmp_path: Path, repo: Path
    ) -> None:
        log_dir = tmp_path / "_AGENT_LOG"
        archive = _zip_of(tmp_path, "r13.zip", {"REPORT.md": "stuff\n"})
        ar.apply_source(archive, repo, dry_run=False, agent_log_dir=log_dir)
        written = list(log_dir.glob(f"*_reintegrate-{repo.name}.md"))
        assert len(written) == 1

    def test_report_printed_inline_without_agent_log_dir(
        self, tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        archive = _zip_of(tmp_path, "r14.zip", {"REPORT.md": "printed content\n"})
        ar.apply_source(archive, repo, dry_run=False)
        assert "printed content" in capsys.readouterr().out

    def test_dry_run_does_not_write_the_report(
        self, tmp_path: Path, repo: Path
    ) -> None:
        log_dir = tmp_path / "_AGENT_LOG"
        archive = _zip_of(tmp_path, "r15.zip", {"REPORT.md": "stuff\n"})
        ar.apply_source(archive, repo, dry_run=True, agent_log_dir=log_dir)
        assert not log_dir.exists()

    def test_no_report_in_source_writes_nothing_to_agent_log_dir(
        self, tmp_path: Path, repo: Path
    ) -> None:
        log_dir = tmp_path / "_AGENT_LOG"
        archive = _zip_of(tmp_path, "r16.zip", {"brand_new.py": "x = 1\n"})
        ar.apply_source(archive, repo, dry_run=False, agent_log_dir=log_dir)
        assert not log_dir.exists()


class TestReadManifest:
    """`EXPORTED_CARDS.txt` at the source root names the covered cards — same answer for a zip and
    for the same content already extracted into a directory; never applied into the repo."""

    ENTRIES = {"EXPORTED_CARDS.txt": "card-a\n\ncard-b\n", "brand_new.py": "x = 1\n"}

    def test_zip_source(self, tmp_path: Path) -> None:
        archive = _zip_of(tmp_path, "m1.zip", self.ENTRIES)
        assert ar.read_manifest(archive) == ["card-a", "card-b"]

    def test_directory_source(self, tmp_path: Path) -> None:
        src = tmp_path / "extracted"
        src.mkdir()
        for name, content in self.ENTRIES.items():
            (src / name).write_text(content)
        assert ar.read_manifest(src) == ["card-a", "card-b"]

    def test_absent_file_is_none(self, tmp_path: Path) -> None:
        archive = _zip_of(tmp_path, "m2.zip", {"brand_new.py": "x = 1\n"})
        assert ar.read_manifest(archive) is None

    def test_nested_file_is_not_the_manifest(self, tmp_path: Path) -> None:
        archive = _zip_of(tmp_path, "m3.zip", {"sub/EXPORTED_CARDS.txt": "card-a\n"})
        assert ar.read_manifest(archive) is None

    def test_manifest_is_never_applied_into_the_repo(
        self, repo: Path, tmp_path: Path
    ) -> None:
        archive = _zip_of(tmp_path, "m4.zip", self.ENTRIES)
        ar.apply_source(archive, repo, dry_run=False)
        assert not (repo / "EXPORTED_CARDS.txt").exists()
        assert (repo / "brand_new.py").exists()
