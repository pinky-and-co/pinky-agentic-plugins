#!/usr/bin/env python3
"""Tests for plugins/pc-delegate/skills/reintegrate/scripts/close_cards.py

Defines "done": the covered cards come from the return's slugs file (zip or directory alike), or
from the repo's `exported:` cards when it is absent; marking a repo as reintegrated writes the
`reintegrated:` frontmatter field and archives a card only once every repo of its `repos:` is in it.
"""

from __future__ import annotations

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

import close_cards as cc  # noqa: E402

REPO_A = "org/repo-a"
REPO_B = "org/repo-b"


def _card(chantier: Path, slug: str, *, repos: str, extra: str = "") -> Path:
    path = chantier / "3-in-progress" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ndate: 2026-10-09\nrepos: [{repos}]\n{extra}---\n# {slug}\n")
    return path


class TestFrontmatterFields:
    def test_set_list_field_appends_and_keeps_the_body(self) -> None:
        text = "---\nrepos: [org/a]\n---\n# Title\n"
        assert cc.set_list_field(text, "reintegrated", ["org/a"]) == (
            "---\nrepos: [org/a]\nreintegrated: [org/a]\n---\n# Title\n"
        )

    def test_set_list_field_replaces_an_existing_line(self) -> None:
        text = "---\nreintegrated: [org/a]\n---\nbody\n"
        assert cc.set_list_field(text, "reintegrated", ["org/a", "org/b"]) == (
            "---\nreintegrated: [org/a, org/b]\n---\nbody\n"
        )

    def test_read_list_field(self) -> None:
        text = "---\nrepos: [org/a, org/b]\n---\n"
        assert cc.read_list_field(text, "repos") == ["org/a", "org/b"]
        assert cc.read_list_field(text, "reintegrated") == []


class TestCovered:
    def _run(self, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
        monkeypatch.setattr(sys, "argv", ["close_cards.py", *argv])
        return cc.main()

    def test_zip_and_directory_give_the_same_cards(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        archive = tmp_path / "ret.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("EXPORTED_CARDS.txt", "card-a\ncard-b\n")
        extracted = tmp_path / "ret"
        extracted.mkdir()
        (extracted / "EXPORTED_CARDS.txt").write_text("card-a\ncard-b\n")
        outputs = []
        for source in (archive, extracted):
            argv = ["covered", str(source), "--chantier", str(tmp_path)]
            code = self._run([*argv, "--repo-slug", REPO_A], monkeypatch)
            assert code == 0
            outputs.append(capsys.readouterr().out)
        assert outputs == ["card-a\ncard-b\n", "card-a\ncard-b\n"]

    def test_absent_file_lists_the_repos_exported_cards(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        chantier = tmp_path / "chantier"
        _card(chantier, "mine", repos=REPO_A, extra="exported: 2026-10-09\n")
        _card(chantier, "other-repo", repos=REPO_B, extra="exported: 2026-10-09\n")
        _card(chantier, "local", repos=REPO_A)
        source = tmp_path / "ret"
        source.mkdir()
        argv = ["covered", str(source), "--chantier", str(chantier)]
        code = self._run([*argv, "--repo-slug", REPO_A], monkeypatch)
        assert code == 3
        assert capsys.readouterr().out == (
            "no EXPORTED_CARDS.txt in the source — exported cards of this repo:\nmine\n"
        )


class TestMarkReintegrated:
    def test_single_repo_card_is_archivable(self, tmp_path: Path) -> None:
        card = _card(tmp_path, "solo", repos=REPO_A)
        assert cc.mark_reintegrated(tmp_path, REPO_A, "solo") == cc.Closure(True, [])
        assert "reintegrated: [org/repo-a]\n" in card.read_text()

    def test_two_repos_one_returned_keeps_the_card(self, tmp_path: Path) -> None:
        card = _card(tmp_path, "duo", repos=f"{REPO_A}, {REPO_B}")
        assert cc.mark_reintegrated(tmp_path, REPO_A, "duo") == cc.Closure(
            False, [REPO_B]
        )
        assert "reintegrated: [org/repo-a]\n" in card.read_text()

    def test_second_repo_completes_the_card(self, tmp_path: Path) -> None:
        card = _card(tmp_path, "duo", repos=f"{REPO_A}, {REPO_B}")
        cc.mark_reintegrated(tmp_path, REPO_A, "duo")
        assert cc.mark_reintegrated(tmp_path, REPO_B, "duo") == cc.Closure(True, [])
        assert "reintegrated: [org/repo-a, org/repo-b]\n" in card.read_text()

    def test_marking_twice_does_not_duplicate(self, tmp_path: Path) -> None:
        card = _card(tmp_path, "duo", repos=f"{REPO_A}, {REPO_B}")
        cc.mark_reintegrated(tmp_path, REPO_A, "duo")
        cc.mark_reintegrated(tmp_path, REPO_A, "duo")
        assert (
            card.read_text().count(REPO_A) == 2
        )  # once in repos, once in reintegrated

    def test_cli_reports_archive_and_keep_lines(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _card(tmp_path, "solo", repos=REPO_A)
        _card(tmp_path, "duo", repos=f"{REPO_A}, {REPO_B}")
        argv = ["mark", "--chantier", str(tmp_path), "--repo-slug", REPO_A]
        argv += ["--card", "solo", "--card", "duo"]
        monkeypatch.setattr(sys, "argv", ["close_cards.py", *argv])
        assert cc.main() == 0
        assert capsys.readouterr().out == (
            "archive solo\nkeep duo missing: org/repo-b\n"
        )

    def test_block_style_repos_keeps_the_card(self, tmp_path: Path) -> None:
        path = tmp_path / "3-in-progress" / "block.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nrepos:\n  - org/repo-a\n  - org/repo-b\n---\n# block\n")
        closure = cc.mark_reintegrated(tmp_path, REPO_A, "block")
        assert closure.archivable is False
        assert closure.skipped == "no inline repos: list"
        assert "reintegrated" not in path.read_text()

    def test_repo_outside_the_cards_repos_is_skipped(self, tmp_path: Path) -> None:
        card = _card(tmp_path, "solo", repos=REPO_A)
        closure = cc.mark_reintegrated(tmp_path, REPO_B, "solo")
        assert closure.archivable is False
        assert closure.skipped == "repo not in repos:"
        assert "reintegrated" not in card.read_text()

    @pytest.mark.parametrize("slug", ["../outside", "/abs/path", "a/b", ".hidden", ""])
    def test_unsafe_slug_is_skipped_without_touching_disk(
        self, tmp_path: Path, slug: str
    ) -> None:
        outside = tmp_path / "outside.md"
        outside.write_text("---\nrepos: [org/repo-a]\n---\n")
        chantier = tmp_path / "chantier"
        (chantier / "3-in-progress").mkdir(parents=True)
        closure = cc.mark_reintegrated(chantier, REPO_A, slug)
        assert closure.archivable is False
        assert closure.skipped == "invalid slug"
        assert outside.read_text() == "---\nrepos: [org/repo-a]\n---\n"

    def test_card_no_longer_in_progress_is_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "3-in-progress").mkdir()
        closure = cc.mark_reintegrated(tmp_path, REPO_A, "gone")
        assert closure.archivable is False
        assert closure.skipped == "not in 3-in-progress"

    def test_cli_prints_skip_lines(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "3-in-progress").mkdir()
        argv = ["mark", "--chantier", str(tmp_path), "--repo-slug", REPO_A]
        argv += ["--card", "gone", "--card", "../x"]
        monkeypatch.setattr(sys, "argv", ["close_cards.py", *argv])
        assert cc.main() == 0
        assert capsys.readouterr().out == (
            "skip gone: not in 3-in-progress\nskip ../x: invalid slug\n"
        )
