#!/usr/bin/env python3
"""Tests for plugins/pc-delegate/skills/export/scripts/{secrets_filter,build_export}.py

Defines "done": a card selected by id is found across the chantier's columns, moved to
`3-in-progress` with `date:` bumped and an `exported:` marker set (dry-run writes nothing), a
secret-bearing path is never written into the zip (from the repo subtree or `_AGENT_LOG`), a
local absolute path inside an `_AGENT_LOG` entry is redacted rather than shipped verbatim, and an
`_AGENT_LOG` entry ships only when it mentions an exported card's slug and is dated within the
60-day window — zero matching entries means no `_AGENT_LOG` in the zip and an explicit line in
the output, never silence.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPTS = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-delegate"
    / "skills"
    / "export"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

import build_export as be  # noqa: E402
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
    for col in be._COLUMNS:
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
        assert be.find_card(chantier, "my-card") == chantier / "1-todo" / "my-card.md"

    def test_not_found_raises(self, chantier: Path) -> None:
        with pytest.raises(be.ExportError):
            be.find_card(chantier, "missing")

    def test_found_in_two_columns_raises(self, chantier: Path) -> None:
        _write_card(chantier, "1-todo", "dup")
        _write_card(chantier, "0-ideas", "dup")
        with pytest.raises(be.ExportError):
            be.find_card(chantier, "dup")


class TestSetFrontmatterField:
    def test_appends_new_field(self) -> None:
        content = "---\ndate: 2026-01-01\n---\n# Title\n"
        result = be.set_frontmatter_field(content, "exported", "2026-02-02")
        assert "exported: 2026-02-02\n" in result
        assert result.endswith("# Title\n")

    def test_replaces_existing_field(self) -> None:
        content = "---\ndate: 2026-01-01\nexported: 2026-01-15\n---\nbody\n"
        result = be.set_frontmatter_field(content, "exported", "2026-02-02")
        assert result.count("exported:") == 1
        assert "exported: 2026-02-02" in result
        assert "2026-01-15" not in result

    def test_missing_frontmatter_raises(self) -> None:
        with pytest.raises(be.ExportError):
            be.set_frontmatter_field("# no frontmatter\n", "exported", "2026-02-02")

    def test_unclosed_frontmatter_raises(self) -> None:
        with pytest.raises(be.ExportError):
            be.set_frontmatter_field("---\ndate: 2026-01-01\nbody\n", "exported", "x")


class TestExportedContentAndApply:
    def test_exported_content_sets_date_and_marker_without_writing(
        self, chantier: Path
    ) -> None:
        card = _write_card(chantier, "1-todo", "movable")
        content = be._exported_content(card, today="2026-03-03")
        assert "date: 2026-03-03" in content
        assert "exported: 2026-03-03" in content
        assert card.read_text() != content  # nothing written yet

    def test_apply_export_moves_from_todo_to_in_progress(self, chantier: Path) -> None:
        card = _write_card(chantier, "1-todo", "movable")
        content = be._exported_content(card, today="2026-03-03")
        be._apply_export(card, chantier, content)
        dest = chantier / "3-in-progress" / "movable.md"
        assert dest.is_file()
        assert dest.read_text() == content
        assert not card.is_file()

    def test_apply_export_already_in_progress_is_a_no_op_move(
        self, chantier: Path
    ) -> None:
        card = _write_card(chantier, "3-in-progress", "already-there")
        content = be._exported_content(card, today="2026-03-03")
        be._apply_export(card, chantier, content)
        assert card.is_file()
        assert card.read_text() == content


class TestFindCardEscape:
    def test_card_id_with_slash_raises(self, chantier: Path) -> None:
        with pytest.raises(be.ExportError):
            be.find_card(chantier, "../../etc/passwd")

    def test_card_id_with_leading_slash_raises(self, chantier: Path) -> None:
        with pytest.raises(be.ExportError):
            be.find_card(chantier, "/etc/passwd")


class TestChantierTreeListing:
    def test_lists_every_card_across_columns(self, chantier: Path) -> None:
        _write_card(chantier, "1-todo", "a")
        _write_card(chantier, "4-done", "b")
        listing = be.chantier_tree_listing(chantier)
        assert "1-todo/a.md" in listing
        assert "4-done/b.md" in listing


class TestFilteredAgentLog:
    SLUGS = ("export-agent-log-non-filtre-par-pertinence", "second-exported-card")
    REF = date(2026, 9, 30)

    def test_oracle_only_slug_mentioning_entries_inside_the_window_ship(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "_AGENT_LOG"
        log_dir.mkdir()
        j59 = (self.REF - timedelta(days=59)).isoformat()
        j61 = (self.REF - timedelta(days=61)).isoformat()
        (log_dir / f"{j59}_backtick-mention.md").write_text(
            "worked on `export-agent-log-non-filtre-par-pertinence` today\n"
        )
        (log_dir / "2026-09-01_path-mention.md").write_text(
            "see /Users/perrine/Workspaces/pinky/_KANBAN/C25/3-in-progress/"
            "second-exported-card.md\n"
        )
        (log_dir / f"{j61}_bold-mention.md").write_text(
            "**export-agent-log-non-filtre-par-pertinence** kicked off\n"
        )
        (log_dir / "2026-09-29_chantier-token-only.md").write_text(
            "advanced CHANTIER0025, no card named\n"
        )
        (log_dir / "no-date-prefix.md").write_text(
            "mentions export-agent-log-non-filtre-par-pertinence but no dated filename\n"
        )

        assert be.filtered_agent_log(log_dir, self.SLUGS, today=self.REF) == {
            f"_AGENT_LOG/{j59}_backtick-mention.md": (
                "worked on `export-agent-log-non-filtre-par-pertinence` today\n"
            ),
            "_AGENT_LOG/2026-09-01_path-mention.md": "see <redacted-local-path>\n",
        }

    def test_j60_is_still_inside_the_window(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "_AGENT_LOG"
        log_dir.mkdir()
        j60 = (self.REF - timedelta(days=60)).isoformat()
        (log_dir / f"{j60}_mention.md").write_text("`my-card` done\n")
        assert be.filtered_agent_log(log_dir, ("my-card",), today=self.REF) == {
            f"_AGENT_LOG/{j60}_mention.md": "`my-card` done\n"
        }

    def test_future_dated_mention_is_outside_the_preceding_window(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "_AGENT_LOG"
        log_dir.mkdir()
        tomorrow = (self.REF + timedelta(days=1)).isoformat()
        (log_dir / f"{tomorrow}_mention.md").write_text("`my-card` done\n")
        assert be.filtered_agent_log(log_dir, ("my-card",), today=self.REF) == {}

    def test_non_yyyy_mm_dd_prefix_is_skipped_without_raising(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "_AGENT_LOG"
        log_dir.mkdir()
        (log_dir / "2026-13-99_bad-month.md").write_text("`my-card` done\n")
        (log_dir / "note.md").write_text("`my-card` done\n")
        (log_dir / "20260929_basic-format.md").write_text("`my-card` done\n")
        assert be.filtered_agent_log(log_dir, ("my-card",), today=self.REF) == {}

    def test_recent_entry_without_any_exported_slug_is_dropped(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "_AGENT_LOG"
        log_dir.mkdir()
        (log_dir / "2026-09-29_unrelated.md").write_text("work on another chantier\n")
        assert be.filtered_agent_log(log_dir, ("my-card",), today=self.REF) == {}

    def test_denylist_still_applies_to_a_mentioning_entry(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "_AGENT_LOG"
        log_dir.mkdir()
        (log_dir / "2026-09-29_fine.md").write_text("`my-card` fine\n")
        (log_dir / "2026-09-29_aws-credentials.md").write_text(
            "`my-card` but denylisted\n"
        )
        assert be.filtered_agent_log(log_dir, ("my-card",), today=self.REF) == {
            "_AGENT_LOG/2026-09-29_fine.md": "`my-card` fine\n"
        }


class TestResolveRepoFiles:
    def test_denylisted_repo_file_is_split_out_and_reported(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src.py").write_text("x = 1\n")
        (repo / ".env").write_text("SECRET=1\n")  # pragma: allowlist secret
        kept, excluded = be._resolve_repo_files(repo, ["src.py", ".env"])
        assert kept == [repo / "src.py"]
        assert excluded == [".env"]

    def test_repo_file_escaping_repo_root_raises(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        with pytest.raises(be.ExportError):
            be._resolve_repo_files(repo, ["../../etc/passwd"])

    def test_absolute_repo_file_raises(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        with pytest.raises(be.ExportError):
            be._resolve_repo_files(repo, ["/etc/passwd"])

    def test_missing_repo_file_raises(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        with pytest.raises(be.ExportError):
            be._resolve_repo_files(repo, ["nope.py"])


class TestBuildZip:
    def test_zip_contains_cards_tree_log_and_guide(self, tmp_path: Path) -> None:
        out = tmp_path / "export.zip"
        be.build_zip(
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

    def test_zip_root_carries_the_exported_slugs_file(self, tmp_path: Path) -> None:
        out = tmp_path / "export.zip"
        be.build_zip(
            out,
            cards={"card-a.md": "a\n", "card-b.md": "b\n"},
            chantier_tree="",
            agent_log={},
            guide_content=None,
            repo=None,
            repo_paths=[],
        )
        with zipfile.ZipFile(out) as zf:
            assert zf.read(be.EXPORTED_CARDS_FILE).decode() == "card-a\ncard-b\n"

    def test_card_content_local_paths_are_redacted(self, tmp_path: Path) -> None:
        out = tmp_path / "export.zip"
        be.build_zip(
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
        out = tmp_path / "export.zip"

        be.build_zip(
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
        prompt = be.suggested_prompt(["card-a", "card-b"])
        assert "card-a" in prompt
        assert "card-b" in prompt

    def test_mentions_report_md_and_zip_layout(self) -> None:
        prompt = be.suggested_prompt(["card-a"])
        assert "REPORT.md" in prompt
        assert "AGENTS.md" in prompt

    def test_full_prompt_for_one_card(self) -> None:
        assert be.suggested_prompt(["card-a"]) == (
            "I've uploaded a zip containing AGENTS.md (read it first — it's the instructions) and "
            "cards/<id>.md for each card (card-a) — the exact task is described there.\n"
            "\n"
            "If a hook or test covering a file you changed is included under repo/ and your "
            "environment can execute code, run it and paste the command, its output, and its "
            "exit code into REPORT.md before you finish.\n"
            "\n"
            "When you're done, give me back a zip containing:\n"
            "- only the files you created or modified, paths preserved exactly as in the archive "
            "you received (not under repo/)\n"
            "- the EXPORTED_CARDS.txt file from the archive you received, at the root of the zip, "
            "returned exactly as it is\n"
            "- a REPORT.md at the root of the zip: what you did, any scope decisions you made, any "
            "bug/issue you found but left alone because it was out of scope, and anything a human "
            "reviewer should know before trusting the change.\n"
        )

    def test_self_check_reminder_present_regardless_of_card_list(self) -> None:
        prompt = be.suggested_prompt(["x", "y"])
        assert "REPORT.md before you finish" in prompt


class TestHookInclusionByRepoFile:
    def test_repo_file_lands_at_its_real_nested_path_in_the_zip(
        self, tmp_path: Path, chantier: Path
    ) -> None:
        """`--repo-file` places a nested hook or test script at its real repo-relative path under
        `repo/`, through the same generic mechanism as any other repo file."""
        _write_card(chantier, "1-todo", "touches-req-ids")
        repo = tmp_path / "repo"
        (repo / "scripts" / "hooks").mkdir(parents=True)
        (repo / "src.py").write_text("x = 1\n")
        (repo / "scripts" / "hooks" / "check_requirement_ids.py").write_text("# hook\n")
        out = tmp_path / "export.zip"

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "build_export.py"),
                "--chantier",
                str(chantier),
                "--card",
                "touches-req-ids",
                "--repo",
                str(repo),
                "--repo-file",
                "src.py",
                "--repo-file",
                "scripts/hooks/check_requirement_ids.py",
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        assert "repo/src.py" in names
        assert "repo/scripts/hooks/check_requirement_ids.py" in names


class TestGuideHooksCarveOut:
    GUIDE_PATH = (
        Path(__file__).parent.parent
        / "plugins"
        / "pc-delegate"
        / "skills"
        / "export"
        / "references"
        / "GUIDE.md"
    )

    def test_carves_out_running_an_included_hook_or_test(self) -> None:
        text = self.GUIDE_PATH.read_text(encoding="utf-8")
        normalized = " ".join(text.split())
        assert "REPORT.md" in text
        assert "hook" in text.lower()
        assert "exit code" in text
        assert "pass the file(s) you touched as its argument" in normalized
        assert (
            "unless editing that hook or test is what your task actually asked for"
            in normalized
        )
        assert "pytest" not in text

    def test_asks_for_the_slugs_file_back_untouched(self) -> None:
        text = " ".join(self.GUIDE_PATH.read_text(encoding="utf-8").split())
        assert "EXPORTED_CARDS.txt" in text
        assert "returned exactly as it is" in text

    def test_does_not_promise_the_full_suite_runs_here(self) -> None:
        text = self.GUIDE_PATH.read_text(encoding="utf-8")
        assert "does not replace the reintegration step" in text
        assert "runs the project's own checks in full" in text


class TestAgentLogZeroMatchLine:
    """Zero `_AGENT_LOG` match: the zip ships without it and the script says so explicitly, in
    dry-run as in a real run — never silence."""

    def _log_dir(self, tmp_path: Path) -> Path:
        log_dir = tmp_path / "_AGENT_LOG"
        log_dir.mkdir()
        (log_dir / f"{date.today().isoformat()}_unrelated.md").write_text(
            "other work\n"
        )
        return log_dir

    def test_dry_run_zero_match_prints_an_explicit_line(
        self, tmp_path: Path, chantier: Path
    ) -> None:
        _write_card(chantier, "1-todo", "quiet-card")
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "build_export.py"),
                "--chantier",
                str(chantier),
                "--card",
                "quiet-card",
                "--agent-log-dir",
                str(self._log_dir(tmp_path)),
                "--out",
                str(tmp_path / "export.zip"),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "0 agent log file(s)" in result.stdout
        assert "would ship no _AGENT_LOG entry" in result.stdout
        assert not (tmp_path / "export.zip").exists()

    def test_real_run_zero_match_ships_no_agent_log_and_says_so(
        self, tmp_path: Path, chantier: Path
    ) -> None:
        _write_card(chantier, "1-todo", "quiet-card")
        out = tmp_path / "export.zip"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "build_export.py"),
                "--chantier",
                str(chantier),
                "--card",
                "quiet-card",
                "--agent-log-dir",
                str(self._log_dir(tmp_path)),
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "shipped no _AGENT_LOG entry" in result.stdout
        with zipfile.ZipFile(out) as zf:
            assert not [n for n in zf.namelist() if n.startswith("_AGENT_LOG/")]

    def test_real_run_with_a_mention_ships_that_entry(
        self, tmp_path: Path, chantier: Path
    ) -> None:
        _write_card(chantier, "1-todo", "loud-card")
        log_dir = tmp_path / "_AGENT_LOG"
        log_dir.mkdir()
        entry = f"{date.today().isoformat()}_mention.md"
        (log_dir / entry).write_text("worked on `loud-card`\n")
        out = tmp_path / "export.zip"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "build_export.py"),
                "--chantier",
                str(chantier),
                "--card",
                "loud-card",
                "--agent-log-dir",
                str(log_dir),
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "shipped no _AGENT_LOG entry" not in result.stdout
        with zipfile.ZipFile(out) as zf:
            assert [n for n in zf.namelist() if n.startswith("_AGENT_LOG/")] == [
                f"_AGENT_LOG/{entry}"
            ]


class TestMainAtomicity:
    def test_repo_file_escape_leaves_board_and_output_untouched(
        self, tmp_path: Path, chantier: Path
    ) -> None:
        card = _write_card(chantier, "1-todo", "safe-card")
        repo = tmp_path / "repo"
        repo.mkdir()
        out = tmp_path / "export.zip"

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "build_export.py"),
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
        out = tmp_path / "export.zip"

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "build_export.py"),
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
