#!/usr/bin/env python3
"""Tests for plugins/pc-kanban/skills/kanban/scripts/open_cards_brief.py."""

import subprocess
import sys
from pathlib import Path

SCRIPTS = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-kanban"
    / "skills"
    / "kanban"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

import open_cards_brief as ocb  # noqa: E402

GOAL_CARD = """---
date: 2026-08-09
sert: null
---
# Explorer `/goal` + équipes d'agents pour l'exécution des cards

> Partiellement livré : `/goal` est câblé dans `chantier`.
> Reste le volet équipes d'agents.

## L'idée

Travailler en subagents, card = objectif.
"""


def _chantier(tmp_path: Path) -> Path:
    """A chantier with one card per column, open and closed."""
    root = tmp_path / "CHANTIER0099-demo"
    cards = {
        "0-ideas/goal-agent-teams.md": GOAL_CARD,
        "1-todo/b-todo.md": "# B à faire\n\nCorriger le parseur.\n",
        "1-todo/a-todo.md": "# A à faire\n\nAjouter un test.\n",
        "2-selected-for-dev/sel.md": "# Sélectionnée\n\nLivrer le hook.\n",
        "3-in-progress/wip.md": "# En cours\n\nNe doit pas apparaître.\n",
        "4-done/done.md": "# Finie\n\nNe doit pas apparaître.\n",
    }
    for rel, text in cards.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_open_cards_lists_the_three_open_columns_in_order(tmp_path):
    briefs = ocb.open_cards(_chantier(tmp_path))
    assert [(b.column, b.name) for b in briefs] == [
        ("0-ideas", "goal-agent-teams.md"),
        ("1-todo", "a-todo.md"),
        ("1-todo", "b-todo.md"),
        ("2-selected-for-dev", "sel.md"),
    ]


def test_goal_is_the_first_block_after_the_title_frontmatter_and_quote_marks_stripped(
    tmp_path,
):
    brief = ocb.open_cards(_chantier(tmp_path))[0]
    assert (
        brief.title == "Explorer `/goal` + équipes d'agents pour l'exécution des cards"
    )
    assert brief.goal == (
        "Partiellement livré : `/goal` est câblé dans `chantier`. "
        "Reste le volet équipes d'agents."
    )


def test_long_goal_is_cut_with_an_ellipsis(tmp_path):
    card = tmp_path / "CHANTIER0099-demo" / "0-ideas" / "long.md"
    card.parent.mkdir(parents=True)
    card.write_text("# Long\n\n" + "mot " * 200 + "\n", encoding="utf-8")
    goal = ocb.card_brief(card).goal
    assert len(goal) == ocb.GOAL_CHARS
    assert goal.endswith("…")


def test_render_open_cards():
    briefs = [
        ocb.CardBrief("0-ideas", "x.md", "Titre X", "Objectif X."),
        ocb.CardBrief("1-todo", "y.md", "Titre Y", ""),
    ]
    assert ocb.render_open_cards(briefs) == (
        "0-ideas/x.md — Titre X\n"
        "    Objectif X.\n"
        "1-todo/y.md — Titre Y\n"
        "    (no goal paragraph)"
    )


def test_cli_prints_nothing_for_a_chantier_with_no_open_card(tmp_path):
    root = tmp_path / "CHANTIER0099-demo" / "4-done"
    root.mkdir(parents=True)
    (root / "done.md").write_text("# Finie\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "open_cards_brief.py"), str(root.parent)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""
