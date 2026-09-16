"""Tests for plugins/pc-kanban/skills/chantier/scripts/drop_card.sh.

Subprocess-driven, mirroring `archive_card.sh`'s own lack of a Python wrapper by giving it one
this time — the script is new, no prior coverage to preserve.
"""

import subprocess
from pathlib import Path

SCRIPT = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-kanban"
    / "skills"
    / "chantier"
    / "scripts"
    / "drop_card.sh"
)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def _card(
    kanban: Path, chantier: str, column: str, name: str, body: str = "# x\n"
) -> Path:
    col = kanban / f"CHANTIER{chantier}-slug" / column
    col.mkdir(parents=True, exist_ok=True)
    card = col / name
    card.write_text(body, encoding="utf-8")
    return card


def test_drops_a_todo_card_into_dropped_archive_with_reason_appended(tmp_path):
    kanban = tmp_path / "_KANBAN"
    card = _card(kanban, "0025", "1-todo", "foo.md", "# Foo\nbody\n")

    result = _run(str(card), "superseded by bar, same need")

    assert result.returncode == 0, result.stderr
    dest = kanban / "_ARCHIVE" / "CHANTIER0025-dropped"
    entries = list(dest.glob("*_foo.md"))
    assert len(entries) == 1
    assert not card.exists()
    content = entries[0].read_text(encoding="utf-8")
    assert "## Dropped (" in content
    assert "superseded by bar, same need" in content


def test_seeds_nav_yml_once(tmp_path):
    kanban = tmp_path / "_KANBAN"
    card = _card(kanban, "0025", "0-ideas", "a.md")
    _run(str(card), "raison")
    nav = kanban / "_ARCHIVE" / "CHANTIER0025-dropped" / ".nav.yml"
    assert nav.is_file()


def test_refuses_a_4_done_card_source(tmp_path):
    kanban = tmp_path / "_KANBAN"
    card = _card(kanban, "0025", "4-done", "done.md")
    result = _run(str(card), "raison")
    assert result.returncode != 0
    assert "not in a droppable column" in result.stderr
    assert card.exists()


def test_refuses_with_no_arguments_at_all():
    """Sous `set -u`, accéder à `$1` sans argument échoue en `unbound variable` avant même les
    messages d'usage si le compte d'arguments n'est pas validé en premier — régression trouvée en
    revue adversariale."""
    result = _run()
    assert result.returncode != 0
    assert "unbound variable" not in result.stderr
    assert "card path and a reason are required" in result.stderr


def test_refuses_without_a_reason(tmp_path):
    kanban = tmp_path / "_KANBAN"
    card = _card(kanban, "0025", "1-todo", "foo.md")
    result = _run(str(card))
    assert result.returncode != 0
    assert "card path and a reason are required" in result.stderr
    assert card.exists()


def test_refuses_with_an_empty_reason_string(tmp_path):
    kanban = tmp_path / "_KANBAN"
    card = _card(kanban, "0025", "1-todo", "foo.md")
    result = _run(str(card), "")
    assert result.returncode != 0
    assert "a reason is required" in result.stderr
    assert card.exists()


def test_refuses_missing_card(tmp_path):
    result = _run(str(tmp_path / "nope.md"), "raison")
    assert result.returncode != 0
    assert "no such card" in result.stderr


def test_refuses_to_overwrite_an_existing_archive_entry(tmp_path):
    kanban = tmp_path / "_KANBAN"
    card = _card(kanban, "0025", "1-todo", "foo.md")
    dest_dir = kanban / "_ARCHIVE" / "CHANTIER0025-dropped"
    dest_dir.mkdir(parents=True)
    import datetime

    today = datetime.date.today().isoformat()
    (dest_dir / f"{today}_foo.md").write_text("already here", encoding="utf-8")

    result = _run(str(card), "raison")
    assert result.returncode != 0
    assert "refusing to overwrite" in result.stderr
    assert card.exists()
