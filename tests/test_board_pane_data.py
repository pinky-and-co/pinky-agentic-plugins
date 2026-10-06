"""Tests for plugins/pc-kanban/hooks/board-pane/board_data.py, on a synthetic board."""

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

_HOOKS = Path(__file__).parent.parent / "plugins" / "pc-kanban" / "hooks"
sys.path.insert(0, str(_HOOKS))
sys.path.insert(0, str(_HOOKS / "board-pane"))

import board_data as bd  # noqa: E402
import stop_handoff as sh  # noqa: E402

SCRIPT = _HOOKS / "board-pane" / "board_data.py"
TODAY = date(2026, 10, 6)

CARD_FULL = """---
repos: [acme/alpha, acme/beta]
model: sonnet
sert: null
priority: 4
date: 2026-10-01
---
Intro line before the title.

# Build the widget

## Done when

The WIDGET renders.

<!-- auto-handoff:start (maintained by stop_handoff.py — do not edit by hand) -->
## Handoff (auto)
- Repo / branch: `acme/alpha` @ `main`
<!-- auto-handoff:end -->
"""

CARD_BARE = "No frontmatter and no heading.\n"
CARD_EMPTY_REPOS = "---\nrepos: []\ndate: not-a-date\n---\n# Orphan\n"


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _repo(path: Path, origin: str | None) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    if origin:
        _git(path, "remote", "add", "origin", origin)
    return path


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace holding a synthetic board with two chantiers, found from its root."""
    root = tmp_path / "ws"
    board = root / "_KANBAN"
    one = board / "CHANTIER0007-first"
    two = board / "CHANTIER0008-second"
    for column in bd.COLUMNS:
        (one / column).mkdir(parents=True)
        (two / column).mkdir(parents=True)
    (one / "story.md").write_text("# Story\nThe widget story.\n")
    (one / "1-todo" / "build-widget.md").write_text(CARD_FULL)
    (one / "4-done" / "finished.md").write_text(CARD_BARE)
    (two / "0-ideas" / "orphan.md").write_text(CARD_EMPTY_REPOS)
    (board / "INDEX.md").write_text("| # | name |\n| 0007 | Widget index |\n")
    (board / "_ARCHIVE" / "0007-done").mkdir(parents=True)
    (board / "_ARCHIVE" / "0007-done" / "old.md").write_text("old widget\nnothing\n")
    (board / "_AGENT_LOG").mkdir()
    (board / "_AGENT_LOG" / "2026-10-01-run.md").write_text("# Run\nwidget log\n")
    monkeypatch.setattr(sh, "SETTINGS_PINKY_PATH", tmp_path / "no-settings.json")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.chdir(root)
    return root


class TestCards:
    def test_every_card_present_once_with_its_column_from_the_folder(self, ws: Path):
        cards = bd.all_cards(ws, TODAY)
        got = sorted((c["slug"], c["column"], c["chantier_id"]) for c in cards)
        assert got == [
            ("build-widget", "1-todo", "0007"),
            ("finished", "4-done", "0007"),
            ("orphan", "0-ideas", "0008"),
        ]

    def test_full_card_oracle(self, ws: Path):
        card = next(c for c in bd.all_cards(ws, TODAY) if c["slug"] == "build-widget")
        board = str(ws / "_KANBAN")
        assert card == {
            "slug": "build-widget",
            "repos": ["acme/alpha", "acme/beta"],
            "board": board,
            "chantier": "CHANTIER0007-first",
            "chantier_id": "0007",
            "column": "1-todo",
            "path": f"{board}/CHANTIER0007-first/1-todo/build-widget.md",
            "title": "Build the widget",
            "frontmatter": {
                "repos": ["acme/alpha", "acme/beta"],
                "model": "sonnet",
                "sert": None,
                "priority": "4",
                "date": "2026-10-01",
            },
            "priority": 4,
            "body": (
                "Intro line before the title.\n\n# Build the widget\n\n## Done when\n\n"
                "The WIDGET renders."
            ),
            "story": f"{board}/CHANTIER0007-first/story.md",
            "age_days": 5,
        }

    def test_card_without_frontmatter_or_heading(self, ws: Path):
        card = next(c for c in bd.all_cards(ws, TODAY) if c["slug"] == "finished")
        assert card["frontmatter"] == {}
        assert card["repos"] is None
        assert card["title"] == "finished"
        assert card["body"] == "No frontmatter and no heading."
        assert card["priority"] == sh.DEFAULT_PRIORITY
        assert card["age_days"] is None

    def test_empty_repos_unparseable_date_and_missing_story(self, ws: Path):
        card = next(c for c in bd.all_cards(ws, TODAY) if c["slug"] == "orphan")
        assert card["frontmatter"]["repos"] == []
        assert card["repos"] == []
        assert card["age_days"] is None
        assert card["story"] is None

    def test_cli_prints_json_for_the_board_found_from_the_cwd(self, ws: Path):
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(ws), "HOME": str(ws / "home")}
        done = subprocess.run(
            [sys.executable, str(SCRIPT), "cards"],
            capture_output=True,
            text=True,
            cwd=ws,
            env=env,
        )
        assert done.returncode == 0, done.stderr
        assert sorted(c["slug"] for c in json.loads(done.stdout)) == [
            "build-widget",
            "finished",
            "orphan",
        ]


class TestSessionRepos:
    def test_normalize_matches_resolve_repos_sh(self):
        assert bd.normalize("git@github.com:acme/alpha.git") == "acme/alpha"
        assert (
            bd.normalize("https://gitlab.example.com/grp/sub/beta.git")
            == "grp/sub/beta"
        )
        assert bd.normalize("http://host/acme/gamma") == "acme/gamma"
        assert bd.normalize("/local/path/repo") == "/local/path/repo"

    def test_inside_a_repo_only_its_origin(self, tmp_path: Path):
        repo = _repo(tmp_path / "alpha", "git@github.com:acme/alpha.git")
        _repo(repo / "vendor" / "inner", "git@github.com:acme/inner.git")
        (repo / "sub").mkdir()
        assert bd.session_repos(repo / "sub") == {
            "in_repo": True,
            "repos": ["acme/alpha"],
        }

    def test_inside_a_repo_without_origin(self, tmp_path: Path):
        repo = _repo(tmp_path / "local", None)
        assert bd.session_repos(repo) == {"in_repo": True, "repos": []}

    def test_outside_every_repo_children_found_bounded_and_deduplicated(
        self, tmp_path: Path
    ):
        root = tmp_path / "root"
        _repo(root / "a", "git@github.com:acme/alpha.git")
        _repo(root / "group" / "b", "https://github.com/acme/beta.git")
        _repo(root / "copy" / "a2", "git@github.com:acme/alpha.git")
        _repo(root / "x" / "y" / "deep", "git@github.com:acme/deep.git")
        _repo(root / "node_modules" / "dep", "git@github.com:acme/skipped.git")
        _repo(root / "noorigin", None)
        assert bd.session_repos(root) == {
            "in_repo": False,
            "repos": ["acme/alpha", "acme/beta", "acme/deep"],
        }

    def test_repo_beyond_the_scan_depth_is_not_found(self, tmp_path: Path):
        root = tmp_path / "root"
        _repo(root / "a" / "b" / "c" / "d", "git@github.com:acme/toodeep.git")
        assert bd.session_repos(root) == {"in_repo": False, "repos": []}

    def test_cli_output(self, tmp_path: Path):
        root = tmp_path / "root"
        _repo(root / "a", "git@github.com:acme/alpha.git")
        done = subprocess.run(
            [sys.executable, str(SCRIPT), "session-repos", str(root)],
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout) == {"in_repo": False, "repos": ["acme/alpha"]}


class TestSearch:
    def test_counts_cover_every_scope_hits_only_the_selected_ones(self, ws: Path):
        result = bd.search(ws, "WiDgEt", ["archive"])
        # kanban: card title, card body line, story, INDEX; archive: 1; logs: 1
        assert result["counts"] == {"kanban": 4, "archive": 1, "logs": 1}
        board = ws / "_KANBAN"
        assert result["hits"] == [
            {
                "scope": "archive",
                "path": str(board / "_ARCHIVE" / "0007-done" / "old.md"),
                "rel": "_ARCHIVE/0007-done/old.md",
                "line": 1,
                "text": "old widget",
            }
        ]
        assert result["truncated"] is False

    def test_kanban_scope_lists_cards_story_and_index_with_line_numbers(self, ws: Path):
        result = bd.search(ws, "widget", ["kanban"])
        got = sorted((h["rel"], h["line"]) for h in result["hits"])
        assert got == [
            ("CHANTIER0007-first/1-todo/build-widget.md", 10),
            ("CHANTIER0007-first/1-todo/build-widget.md", 14),
            ("CHANTIER0007-first/story.md", 2),
            ("INDEX.md", 2),
        ]

    def test_match_is_a_literal_not_a_pattern(self, ws: Path):
        assert bd.search(ws, "wid.et")["counts"] == {
            "kanban": 0,
            "archive": 0,
            "logs": 0,
        }

    def test_kanban_scope_covers_the_done_column(self, ws: Path):
        result = bd.search(ws, "no heading", ["kanban"])
        assert [h["rel"] for h in result["hits"]] == [
            "CHANTIER0007-first/4-done/finished.md"
        ]

    def test_a_query_starting_with_a_dash_follows_a_double_dash(self, ws: Path):
        (ws / "_KANBAN" / "INDEX.md").write_text("see -dropped here\n")
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(ws), "HOME": str(ws / "home")}
        done = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "search",
                "--scope",
                "kanban",
                "--",
                "-dropped",
            ],
            capture_output=True,
            text=True,
            cwd=ws,
            env=env,
        )
        assert done.returncode == 0, done.stderr
        assert [h["rel"] for h in json.loads(done.stdout)["hits"]] == ["INDEX.md"]

    def test_empty_query_matches_nothing(self, ws: Path):
        assert bd.search(ws, "")["hits"] == []

    def test_scope_argument_parsing(self):
        assert bd.parse_scopes("logs,kanban") == ["kanban", "logs"]
        with pytest.raises(Exception, match="unknown scope: nope"):
            bd.parse_scopes("kanban,nope")

    def test_cli_scope_option(self, ws: Path):
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(ws), "HOME": str(ws / "home")}
        done = subprocess.run(
            [sys.executable, str(SCRIPT), "search", "widget", "--scope", "logs"],
            capture_output=True,
            text=True,
            cwd=ws,
            env=env,
        )
        assert done.returncode == 0, done.stderr
        out = json.loads(done.stdout)
        assert [h["rel"] for h in out["hits"]] == ["_AGENT_LOG/2026-10-01-run.md"]


@pytest.fixture
def session_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A board shaped like the real one for a repo: 0023 live, 0024 archived-only, 0030 foreign."""
    root = tmp_path / "ws"
    board = root / "_KANBAN"
    mine = "---\nrepos: [acme/alpha]\n---\n# Mine\nneedle\n"
    other = "---\nrepos: [acme/zeta]\n---\n# Other\nneedle\n"
    live = board / "CHANTIER0023-live"
    (live / "1-todo").mkdir(parents=True)
    (live / "1-todo" / "mine.md").write_text(mine)
    (live / "story.md").write_text("story needle\n")
    (live / "4-done").mkdir()
    (live / "4-done" / "foreign.md").write_text(other)
    old = board / "_ARCHIVE" / "CHANTIER0024-gone"
    old.mkdir(parents=True)
    (old / "card.md").write_text(mine)
    (old / "story.md").write_text("archived story needle\n")
    (board / "_ARCHIVE" / "CHANTIER0023-live-part").mkdir()
    (board / "_ARCHIVE" / "CHANTIER0023-live-part" / "x.md").write_text("old needle\n")
    foreign = board / "CHANTIER0030-foreign"
    (foreign / "1-todo").mkdir(parents=True)
    (foreign / "1-todo" / "f.md").write_text(other)
    (foreign / "story.md").write_text("foreign story needle\n")
    (board / "_ARCHIVE" / "CHANTIER0031-foreign").mkdir()
    (board / "_ARCHIVE" / "CHANTIER0031-foreign" / "f.md").write_text(other)
    (board / "INDEX.md").write_text("index needle\n")
    (board / "_AGENT_LOG").mkdir()
    (board / "_AGENT_LOG" / "run.md").write_text("log needle\n")
    monkeypatch.setattr(sh, "SETTINGS_PINKY_PATH", tmp_path / "no-settings.json")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.chdir(root)
    return root


class TestLegacyReposCard:
    def test_archived_legacy_card_qualifies_a_chantier_by_bare_dir_name(
        self, tmp_path: Path
    ):
        board = tmp_path / "_KANBAN"
        old = board / "_ARCHIVE" / "CHANTIER0010-legacy"
        old.mkdir(parents=True)
        (old / "card.md").write_text("# Old\n\n**Repos:** `alpha`\nneedle\n")
        assert bd._session_chantiers(board, ["acme/alpha"]) == {"0010"}
        assert bd._session_chantiers(board, ["acme/beta"]) == set()


class TestSearchSessionRepos:
    def _rels(self, result: dict[str, object], scope: str) -> list[str]:
        return sorted(h["rel"] for h in result["hits"] if h["scope"] == scope)  # type: ignore[index,union-attr]

    def test_kanban_covers_qualifying_chantiers_only_and_drops_the_index(
        self, session_board: Path
    ):
        result = bd.search(session_board, "needle", ["kanban"], ["acme/alpha"])
        assert self._rels(result, "kanban") == [
            "CHANTIER0023-live/1-todo/mine.md",
            "CHANTIER0023-live/4-done/foreign.md",
            "CHANTIER0023-live/story.md",
        ]

    def test_archive_covers_the_archive_dirs_of_qualifying_chantiers(
        self, session_board: Path
    ):
        # 0024 qualifies through its archived card alone; 0023's archive dir comes with it.
        result = bd.search(session_board, "needle", ["archive"], ["acme/alpha"])
        assert self._rels(result, "archive") == [
            "_ARCHIVE/CHANTIER0023-live-part/x.md",
            "_ARCHIVE/CHANTIER0024-gone/card.md",
            "_ARCHIVE/CHANTIER0024-gone/story.md",
        ]

    def test_logs_are_never_filtered_and_counts_follow_the_filter(
        self, session_board: Path
    ):
        result = bd.search(session_board, "needle", ["logs"], ["acme/alpha"])
        assert self._rels(result, "logs") == ["_AGENT_LOG/run.md"]
        assert result["counts"] == {"kanban": 3, "archive": 3, "logs": 1}

    def test_no_matching_chantier_leaves_only_the_logs(self, session_board: Path):
        result = bd.search(session_board, "needle", None, ["acme/nobody"])
        assert result["counts"] == {"kanban": 0, "archive": 0, "logs": 1}

    def test_no_repos_means_every_chantier_and_the_index(self, session_board: Path):
        for repos in (None, []):
            result = bd.search(session_board, "needle", None, repos)
            assert result["counts"] == {"kanban": 6, "archive": 4, "logs": 1}
        assert "INDEX.md" in self._rels(bd.search(session_board, "needle"), "kanban")

    def test_cli_repos_option_goes_before_the_double_dash(self, session_board: Path):
        env = {
            **os.environ,
            "CLAUDE_PROJECT_DIR": str(session_board),
            "HOME": str(session_board / "home"),
        }
        done = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "search",
                "--scope",
                "kanban",
                "--repos",
                "acme/alpha,acme/beta",
                "--",
                "needle",
            ],
            capture_output=True,
            text=True,
            cwd=session_board,
            env=env,
        )
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout)["counts"]["kanban"] == 3
