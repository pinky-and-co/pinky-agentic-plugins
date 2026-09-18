#!/usr/bin/env python3
"""Tests for plugins/pc-delegate/skills/aller/scripts/{secrets_filter,build_aller}.py

Defines "done": a card selected by id is found across the chantier's columns, moved to
`3-in-progress` with `date:` bumped and an `exported:` marker set (dry-run writes nothing), a
secret-bearing path is never written into the zip (from the repo subtree or `_AGENT_LOG`), and a
local absolute path inside an `_AGENT_LOG` entry is redacted rather than shipped verbatim.
"""

from __future__ import annotations

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
    / "aller"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

import build_aller as ba  # noqa: E402
import secrets_filter as sf  # noqa: E402


class TestIsDenied:
    @pytest.mark.parametrize(
        "path",
        [
            ".env",
            "config/.env",
            ".env.local",
            "prod.env",
            ".envrc",
            "keys/id_rsa",
            "keys/id_rsa.pub",
            "server.pem",
            "credentials.json",
            "credentials_prod.json",
            "aws-credentials.json",
            "gcp-credentials.json",
            ".aws/credentials",
            ".claude/settings.local.json",
            "secrets/api.key",
            ".ENV",
            "ID_RSA",
            "AWS-CREDENTIALS.JSON",
        ],
    )
    def test_matches(self, path: str) -> None:
        assert sf.is_denied(path) is True

    @pytest.mark.parametrize(
        "path", ["README.md", "src/pkg/module.py", "keys/README.md", "envvars.txt"]
    )
    def test_does_not_match(self, path: str) -> None:
        assert sf.is_denied(path) is False


class TestRedactLocalPaths:
    def test_redacts_macos_home_path(self) -> None:
        text = "see /Users/perrine/Workspaces/pinky/repo/file.py for context"
        result = sf.redact_local_paths(text)
        assert "/Users/perrine" not in result
        assert "<redacted-local-path>" in result

    def test_redacts_linux_home_path(self) -> None:
        text = "see /home/runner/work/repo/file.py for context"
        result = sf.redact_local_paths(text)
        assert "/home/runner" not in result
        assert "<redacted-local-path>" in result

    def test_redacts_root_path(self) -> None:
        text = "ran as /root/.cache/pip/wheel.whl"
        result = sf.redact_local_paths(text)
        assert "/root/.cache" not in result
        assert "<redacted-local-path>" in result

    def test_leaves_unrelated_text_untouched(self) -> None:
        text = "no local path here, just prose"
        assert sf.redact_local_paths(text) == text


@pytest.fixture
def chantier(tmp_path: Path) -> Path:
    root = tmp_path / "CHANTIER0099-test"
    for col in ba._COLUMNS:
        (root / col).mkdir(parents=True)
    return root


def _write_card(
    chantier: Path, column: str, card_id: str, extra_front: str = ""
) -> Path:
    path = chantier / column / f"{card_id}.md"
    path.write_text(
        f"---\ndate: 2026-01-01\nrepos: []\nsert: null\n{extra_front}---\n# {card_id}\n\nbody\n"
    )
    return path


class TestFindCard:
    def test_found_in_one_column(self, chantier: Path) -> None:
        _write_card(chantier, "1-todo", "my-card")
        assert ba.find_card(chantier, "my-card") == chantier / "1-todo" / "my-card.md"

    def test_not_found_raises(self, chantier: Path) -> None:
        with pytest.raises(ba.AllerError):
            ba.find_card(chantier, "missing")

    def test_found_in_two_columns_raises(self, chantier: Path) -> None:
        _write_card(chantier, "1-todo", "dup")
        _write_card(chantier, "0-ideas", "dup")
        with pytest.raises(ba.AllerError):
            ba.find_card(chantier, "dup")


class TestSetFrontmatterField:
    def test_appends_new_field(self) -> None:
        content = "---\ndate: 2026-01-01\n---\n# Title\n"
        result = ba.set_frontmatter_field(content, "exported", "2026-02-02")
        assert "exported: 2026-02-02\n" in result
        assert result.endswith("# Title\n")

    def test_replaces_existing_field(self) -> None:
        content = "---\ndate: 2026-01-01\nexported: 2026-01-15\n---\nbody\n"
        result = ba.set_frontmatter_field(content, "exported", "2026-02-02")
        assert result.count("exported:") == 1
        assert "exported: 2026-02-02" in result
        assert "2026-01-15" not in result

    def test_missing_frontmatter_raises(self) -> None:
        with pytest.raises(ba.AllerError):
            ba.set_frontmatter_field("# no frontmatter\n", "exported", "2026-02-02")

    def test_unclosed_frontmatter_raises(self) -> None:
        with pytest.raises(ba.AllerError):
            ba.set_frontmatter_field("---\ndate: 2026-01-01\nbody\n", "exported", "x")


class TestExportedContentAndApply:
    def test_exported_content_sets_date_and_marker_without_writing(
        self, chantier: Path
    ) -> None:
        card = _write_card(chantier, "1-todo", "movable")
        content = ba._exported_content(card, today="2026-03-03")
        assert "date: 2026-03-03" in content
        assert "exported: 2026-03-03" in content
        assert card.read_text() != content  # nothing written yet

    def test_apply_export_moves_from_todo_to_in_progress(self, chantier: Path) -> None:
        card = _write_card(chantier, "1-todo", "movable")
        content = ba._exported_content(card, today="2026-03-03")
        ba._apply_export(card, chantier, content)
        dest = chantier / "3-in-progress" / "movable.md"
        assert dest.is_file()
        assert dest.read_text() == content
        assert not card.is_file()

    def test_apply_export_already_in_progress_is_a_no_op_move(
        self, chantier: Path
    ) -> None:
        card = _write_card(chantier, "3-in-progress", "already-there")
        content = ba._exported_content(card, today="2026-03-03")
        ba._apply_export(card, chantier, content)
        assert card.is_file()
        assert card.read_text() == content


class TestFindCardEscape:
    def test_card_id_with_slash_raises(self, chantier: Path) -> None:
        with pytest.raises(ba.AllerError):
            ba.find_card(chantier, "../../etc/passwd")

    def test_card_id_with_leading_slash_raises(self, chantier: Path) -> None:
        with pytest.raises(ba.AllerError):
            ba.find_card(chantier, "/etc/passwd")


class TestChantierTreeListing:
    def test_lists_every_card_across_columns(self, chantier: Path) -> None:
        _write_card(chantier, "1-todo", "a")
        _write_card(chantier, "4-done", "b")
        listing = ba.chantier_tree_listing(chantier)
        assert "1-todo/a.md" in listing
        assert "4-done/b.md" in listing


class TestFilteredAgentLog:
    def test_redacts_local_paths(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "_AGENT_LOG"
        log_dir.mkdir()
        (log_dir / "2026-01-01_note.md").write_text(
            "see /Users/perrine/Workspaces/pinky/repo/file.py\n"
        )
        result = ba.filtered_agent_log(log_dir)
        assert "_AGENT_LOG/2026-01-01_note.md" in result
        assert "/Users/perrine" not in result["_AGENT_LOG/2026-01-01_note.md"]

    def test_excludes_a_md_file_that_itself_matches_the_denylist(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "_AGENT_LOG"
        log_dir.mkdir()
        (log_dir / "2026-01-01_note.md").write_text("hello\n")
        (log_dir / "aws-credentials.md").write_text("not really markdown\n")
        result = ba.filtered_agent_log(log_dir)
        assert "_AGENT_LOG/2026-01-01_note.md" in result
        assert "_AGENT_LOG/aws-credentials.md" not in result


class TestResolveRepoFiles:
    def test_denylisted_repo_file_is_split_out_and_reported(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src.py").write_text("x = 1\n")
        (repo / ".env").write_text("SECRET=1\n")  # pragma: allowlist secret
        kept, excluded = ba._resolve_repo_files(repo, ["src.py", ".env"])
        assert kept == [repo / "src.py"]
        assert excluded == [".env"]

    def test_repo_file_escaping_repo_root_raises(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        with pytest.raises(ba.AllerError):
            ba._resolve_repo_files(repo, ["../../etc/passwd"])

    def test_absolute_repo_file_raises(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        with pytest.raises(ba.AllerError):
            ba._resolve_repo_files(repo, ["/etc/passwd"])

    def test_missing_repo_file_raises(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        with pytest.raises(ba.AllerError):
            ba._resolve_repo_files(repo, ["nope.py"])


class TestBuildZip:
    def test_zip_contains_cards_tree_log_and_guide(self, tmp_path: Path) -> None:
        out = tmp_path / "aller.zip"
        ba.build_zip(
            out,
            cards={"my-card.md": "# My card\n"},
            chantier_tree="1-todo/my-card.md\n",
            agent_log={"_AGENT_LOG/note.md": "hello\n"},
            guide_content="# Guide\n",
            repo=None,
            repo_paths=[],
        )
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        assert "cards/my-card.md" in names
        assert "CHANTIER_TREE.txt" in names
        assert "_AGENT_LOG/note.md" in names
        assert "AGENTS.md" in names

    def test_card_content_local_paths_are_redacted(self, tmp_path: Path) -> None:
        out = tmp_path / "aller.zip"
        ba.build_zip(
            out,
            cards={"c.md": "see /Users/perrine/Workspaces/pinky/repo/file.py\n"},
            chantier_tree="",
            agent_log={},
            guide_content=None,
            repo=None,
            repo_paths=[],
        )
        with zipfile.ZipFile(out) as zf:
            content = zf.read("cards/c.md").decode()
        assert "/Users/perrine" not in content
        assert "<redacted-local-path>" in content

    def test_only_pre_resolved_repo_paths_are_written(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src.py").write_text("x = 1\n")
        out = tmp_path / "aller.zip"

        ba.build_zip(
            out,
            cards={},
            chantier_tree="",
            agent_log={},
            guide_content=None,
            repo=repo,
            repo_paths=[repo / "src.py"],
        )

        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        assert "repo/src.py" in names


class TestSuggestedPrompt:
    def test_lists_every_card_id(self) -> None:
        prompt = ba.suggested_prompt(["card-a", "card-b"])
        assert "card-a" in prompt
        assert "card-b" in prompt

    def test_mentions_report_md_and_zip_layout(self) -> None:
        prompt = ba.suggested_prompt(["card-a"])
        assert "REPORT.md" in prompt
        assert "AGENTS.md" in prompt


class TestMainAtomicity:
    def test_repo_file_escape_leaves_board_and_output_untouched(
        self, tmp_path: Path, chantier: Path
    ) -> None:
        card = _write_card(chantier, "1-todo", "safe-card")
        repo = tmp_path / "repo"
        repo.mkdir()
        out = tmp_path / "aller.zip"

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "build_aller.py"),
                "--chantier",
                str(chantier),
                "--card",
                "safe-card",
                "--repo",
                str(repo),
                "--repo-file",
                "../../etc/passwd",
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 2
        assert card.is_file()
        assert not (chantier / "3-in-progress" / "safe-card.md").exists()
        assert not out.exists()

    def test_successful_run_moves_card_and_writes_zip(
        self, tmp_path: Path, chantier: Path
    ) -> None:
        card = _write_card(chantier, "1-todo", "movable")
        out = tmp_path / "aller.zip"

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "build_aller.py"),
                "--chantier",
                str(chantier),
                "--card",
                "movable",
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert not card.is_file()
        assert (chantier / "3-in-progress" / "movable.md").is_file()
        assert out.is_file()
        assert "REPORT.md" in result.stdout
        assert "movable" in result.stdout
