"""Tests for the classifier-written `model:`/`effort:` of a card (select and grill's step)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

KANBAN = Path(__file__).parent.parent / "plugins" / "pc-kanban" / "hooks"
CARD_OPS = KANBAN / "card-ops" / "pc_kanban_card_ops.py"
ROUTING = KANBAN / "card-routing" / "pc_kanban_card_routing.py"

FAKE_OK = (
    "import json, sys\n"
    "sys.stdin.read()\n"
    "print(json.dumps({'category': 'routine', 'model': 'haiku', 'effort': 'low',"
    " 'model_id': 'fake', 'probabilities': {'routine': 1.0}}))\n"
)
FAKE_ERROR = (
    "import json, sys\n"
    "sys.stdin.read()\n"
    "print(json.dumps({'category': 'logic', 'model': 'sonnet', 'effort': 'medium',"
    " 'model_id': 'fake', 'error': 'HTTP 507'}))\n"
)
DEFINER = (
    "# D\n\n## Requirements\n\n| Id | P | E | C |\n|---|---|---|---|\n"
    "| PKG-0001-01 | MUST | x | y |\n"
)


def _fake_core(root: Path, script: str) -> dict[str, str]:
    classify = root / "pc-core" / "hooks" / "subagent-routing" / "classify.py"
    classify.parent.mkdir(parents=True)
    classify.write_text(script, encoding="utf-8")
    return {**os.environ, "PC_CORE_ROOT": str(root / "pc-core")}


def _card(tmp_path: Path, head: str) -> Path:
    card = tmp_path / "board" / "CHANTIER0001-x" / "1-todo" / "demo.md"
    card.parent.mkdir(parents=True)
    card.write_text(
        f"---\ndate: 2026-01-01\n{head}---\n# Demo\n\n## Done when\n\nx\n",
        encoding="utf-8",
    )
    definer = card.parent.parent / "0-ideas" / "definer.md"
    definer.parent.mkdir()
    definer.write_text(DEFINER, encoding="utf-8")
    return card


def _select(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CARD_OPS),
            "--board",
            str(tmp_path / "board"),
            "select",
            "demo",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_select_without_model_writes_the_classifier_values_then_moves(
    tmp_path: Path,
) -> None:
    card = _card(tmp_path, "requirements: [PKG-0001-01]\n")
    result = _select(tmp_path, _fake_core(tmp_path, FAKE_OK))
    dest = card.parent.parent / "2-selected-for-dev" / "demo.md"
    assert result.returncode == 0, result.stderr
    assert not card.exists()
    text = dest.read_text()
    assert "model: haiku\n" in text
    assert "effort: low\n" in text


def test_select_keeps_a_model_already_written(tmp_path: Path) -> None:
    _card(tmp_path, "model: opus\nrequirements: [PKG-0001-01]\n")
    result = _select(tmp_path, _fake_core(tmp_path, FAKE_OK))
    dest = tmp_path / "board" / "CHANTIER0001-x" / "2-selected-for-dev" / "demo.md"
    assert result.returncode == 0, result.stderr
    assert "model: opus\n" in dest.read_text()
    assert "haiku" not in dest.read_text()


def test_select_refuses_on_a_classifier_error_and_leaves_the_card(
    tmp_path: Path,
) -> None:
    card = _card(tmp_path, "requirements: [PKG-0001-01]\n")
    before = card.read_text()
    result = _select(tmp_path, _fake_core(tmp_path, FAKE_ERROR))
    assert result.returncode == 1
    assert "HTTP 507" in result.stderr
    assert card.read_text() == before


def test_select_refuses_when_the_classifier_is_missing(tmp_path: Path) -> None:
    card = _card(tmp_path, "requirements: [PKG-0001-01]\n")
    before = card.read_text()
    env = {"PATH": "/usr/bin:/bin", "PC_CORE_ROOT": str(tmp_path / "absent")}
    result = _select(tmp_path, env)
    assert result.returncode == 1
    assert card.read_text() == before


def test_grill_step_writes_model_and_effort_over_existing_values(
    tmp_path: Path,
) -> None:
    card = _card(tmp_path, "model: opus\neffort: high\nrequirements: [PKG-0001-01]\n")
    env = _fake_core(tmp_path, FAKE_OK)
    result = subprocess.run(
        [sys.executable, str(ROUTING), str(card)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    text = card.read_text()
    assert text.count("model:") == 1 and "model: haiku\n" in text
    assert text.count("effort:") == 1 and "effort: low\n" in text
    assert "requirements: [PKG-0001-01]\n" in text
    assert "# Demo" in text


def test_grill_step_error_leaves_the_card_unchanged(tmp_path: Path) -> None:
    card = _card(tmp_path, "requirements: [PKG-0001-01]\n")
    before = card.read_text()
    result = subprocess.run(
        [sys.executable, str(ROUTING), str(card)],
        capture_output=True,
        text=True,
        check=False,
        env=_fake_core(tmp_path, FAKE_ERROR),
    )
    assert result.returncode == 1
    assert card.read_text() == before


def test_an_unclosed_frontmatter_gets_its_closing_line(tmp_path: Path) -> None:
    card = tmp_path / "demo.md"
    card.write_text("---\ndate: 2026-01-01", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROUTING), str(card)],
        capture_output=True,
        text=True,
        check=False,
        env=_fake_core(tmp_path, FAKE_OK),
    )
    assert result.returncode == 0, result.stderr
    assert card.read_text() == "---\ndate: 2026-01-01\nmodel: haiku\neffort: low\n---\n"


def test_grill_step_on_a_bom_card_keeps_one_frontmatter(tmp_path: Path) -> None:
    card = _card(tmp_path, "requirements: [PKG-0001-01]\n")
    card.write_text("\ufeff" + card.read_text(encoding="utf-8"), encoding="utf-8")
    seen = tmp_path / "stdin.txt"
    script = FAKE_OK.replace(
        "sys.stdin.read()\n", f"open({str(seen)!r}, 'w').write(sys.stdin.read())\n"
    )
    result = subprocess.run(
        [sys.executable, str(ROUTING), str(card)],
        capture_output=True,
        text=True,
        check=False,
        env=_fake_core(tmp_path, script),
    )
    assert result.returncode == 0, result.stderr
    text = card.read_text(encoding="utf-8")
    assert text.startswith("---\ndate: 2026-01-01\n")
    assert text.count("---\n") == 2
    assert "model: haiku\n" in text and "effort: low\n" in text
    assert "requirements: [PKG-0001-01]\n" in text
    assert "date: 2026-01-01" not in seen.read_text(encoding="utf-8")


def test_an_edit_made_while_classifying_survives(tmp_path: Path) -> None:
    card = _card(tmp_path, "requirements: [PKG-0001-01]\n")
    script = FAKE_OK.replace(
        "sys.stdin.read()\n",
        f"sys.stdin.read()\nwith open({str(card)!r}, 'a') as f: f.write('EDITED-MIDWAY\\n')\n",
    )
    result = subprocess.run(
        [sys.executable, str(ROUTING), str(card)],
        capture_output=True,
        text=True,
        check=False,
        env=_fake_core(tmp_path, script),
    )
    assert result.returncode == 0, result.stderr
    text = card.read_text(encoding="utf-8")
    assert "EDITED-MIDWAY" in text
    assert "model: haiku\n" in text and "effort: low\n" in text
