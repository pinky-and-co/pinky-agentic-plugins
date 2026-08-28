"""subagentStatusLine — one custom row per subagent in the agents panel (2026-08-12).

Companion to test_statusline.py, same oracle strategy (tests.md § generator oracle): unit
oracles on the small pure helpers, then end-to-end oracles on `render_task()` — the pure entry
point extracted from `main()`'s per-task loop by this same fix, for the same reason (a testable
seam, and a real bug closed: a single malformed task's `tokenCount`/`contextWindowSize` being the
wrong TYPE used to crash the whole render loop via an uncaught `int(str / str)`, blanking every
other agent's row too — `main()` now isolates each task's render in a try/except, and
`render_task()` itself falls back to the dim `ctx:—` placeholder on a type mismatch instead of
trusting the schema blindly.
"""

import json
import sys
from pathlib import Path

HOOKS = Path(__file__).parent.parent / "plugins" / "pc-statusline" / "scripts"
sys.path.insert(0, str(HOOKS))

import subagent_statusline as sal  # noqa: E402


# --- small pure helpers -------------------------------------------------------------------


def test_color_for_pct_thresholds():
    assert sal._color_for_pct(0) == sal.GREEN
    assert sal._color_for_pct(69) == sal.GREEN
    assert sal._color_for_pct(70) == sal.YELLOW
    assert sal._color_for_pct(89) == sal.YELLOW
    assert sal._color_for_pct(90) == sal.RED


def test_mini_bar_oracle():
    # 40% of a 5-char bar = round(2.0) = 2 filled, 3 empty.
    assert sal._mini_bar(40, width=5) == (
        f"{sal.GREEN}{'█' * 2}{sal.RESET}{sal.DIM}{'░' * 3}{sal.RESET}"
    )


def test_mini_bar_clamps_out_of_range_pct():
    assert sal._mini_bar(150, width=5) == sal._mini_bar(100, width=5)
    assert sal._mini_bar(-10, width=5) == sal._mini_bar(0, width=5)


def test_short_model_oracle():
    assert sal._short_model("claude-haiku-4-5-20251001") == "Haiku 4.5"
    assert sal._short_model("claude-sonnet-5") == "Sonnet 5"
    # Diverges from statusline.py's _short_model on the falsy case: "?" here, None there —
    # each module renders its own placeholder inline, so this is what THIS module actually does.
    assert sal._short_model(None) == "?"
    assert sal._short_model("") == "?"


def test_visible_len_ignores_ansi_but_does_not_width_correct_emoji():
    # Unlike statusline.py's _visible_len, this one has no wide-char table — every non-escaped
    # Python character counts as 1, emoji included. Documenting the actual behaviour, not the
    # other module's: the two are independent implementations, never shared, never compared.
    assert sal._visible_len(f"{sal.RED}ab{sal.RESET}") == 2
    assert sal._visible_len("🤖") == 1
    assert sal._visible_len("ab") == 2


# --- render_task() oracles -----------------------------------------------------------------


def _strip_ansi(s: str) -> str:
    import re

    return re.sub(r"\033\[[0-9;]*m", "", s)


def test_render_task_returns_none_without_an_id():
    assert sal.render_task({}, columns=80) is None
    assert sal.render_task({"name": "x"}, columns=80) is None


def test_render_task_oracle_full_task_readable_content():
    task = {
        "id": "t1",
        "name": "worker",
        "status": "running",
        "model": "claude-haiku-4-5",
        "effort": "high",
        "tokenCount": 5000,
        "contextWindowSize": 200000,
    }
    line = sal.render_task(task, columns=80)
    assert line["id"] == "t1"
    stripped = _strip_ansi(line["content"])
    assert stripped.startswith("Running ⚙️ ")
    assert "worker" in stripped
    assert "🤖 Haiku 4.5" in stripped
    assert "💪 : high" in stripped
    assert "2%" in stripped  # 5000 / 200000 = 2.5% -> int() truncates to 2


def test_render_task_unresolved_model_shows_dim_ctx_placeholder():
    """contextWindowSize/tokenCount are omitted while the task's model isn't resolved yet
    (documented harness behaviour, not a malformed payload) — must degrade quietly, not guess."""
    task = {"id": "t1", "name": "worker", "status": "pending"}
    stripped = _strip_ansi(sal.render_task(task, columns=80)["content"])
    assert "ctx:—" in stripped
    assert "🤖 ?" in stripped  # model unresolved too


def test_render_task_tolerates_wrong_typed_token_fields():
    """The bug this fix closes: tokenCount/contextWindowSize holding the wrong TYPE (a string,
    from a misbehaving upstream) used to raise inside int(token_count / ctx_size * 100) with
    nothing catching it in main()'s loop. render_task() now type-checks before dividing."""
    task = {
        "id": "t1",
        "name": "worker",
        "tokenCount": "not-a-number",
        "contextWindowSize": 200000,
    }
    stripped = _strip_ansi(sal.render_task(task, columns=80)["content"])
    assert "ctx:—" in stripped


def test_render_task_truncates_a_long_name_to_the_column_budget():
    """Oracle on the exact truncation slice — the fixed-cost segments (status emoji, model,
    effort, ctx placeholder) leave a 6-char name budget at columns=30 for this task shape, so a
    50-char name must come back as exactly 5 chars + the ellipsis."""
    task = {"id": "t1", "name": "A" * 50, "status": ""}
    stripped = _strip_ansi(sal.render_task(task, columns=30)["content"])
    assert "AAAAA…" in stripped
    assert "A" * 6 not in stripped  # the 6th A must be gone, not just visually clipped


def test_render_task_leaves_a_short_name_untouched():
    task = {"id": "t1", "name": "ok", "status": "completed"}
    stripped = _strip_ansi(sal.render_task(task, columns=80)["content"])
    assert "ok" in stripped
    assert "…" not in stripped


def test_render_task_unknown_status_falls_back_to_default_emoji():
    task = {"id": "t1", "name": "worker", "status": "some-future-status"}
    stripped = _strip_ansi(sal.render_task(task, columns=80)["content"])
    assert "▪️" in stripped


# --- main() end-to-end: one bad task must not blank the others -----------------------------


def test_render_task_tolerates_a_dict_where_a_number_was_expected():
    """contextWindowSize/tokenCount holding a wrong-shaped VALUE (not just a wrong scalar type)
    must also degrade via the isinstance guard, not just plain strings."""
    task = {
        "id": "t1",
        "name": "worker",
        "tokenCount": "garbage",
        "contextWindowSize": {"x": 1},
    }
    stripped = _strip_ansi(sal.render_task(task, columns=80)["content"])
    assert "ctx:—" in stripped


def test_main_skips_a_non_dict_task_but_still_renders_the_rest():
    """The real invariant: main() only calls render_task() on dict-shaped tasks, and wraps that
    call in its own try/except — so one adversarial entry in the tasks array (not even a dict)
    degrades to Claude Code's default row for that slot instead of silencing every other agent's
    status on the same refresh tick."""
    import subprocess

    payload = {
        "columns": 80,
        "tasks": [
            {"id": "good-1", "name": "alpha", "status": "running"},
            "not-a-task-object",
            {"id": "good-2", "name": "gamma", "status": "completed"},
        ],
    }
    result = subprocess.run(
        [sys.executable, str(HOOKS / "subagent_statusline.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert result.returncode == 0
    out_ids = set()
    for line in result.stdout.splitlines():
        out_ids.add(json.loads(line)["id"])
    assert out_ids == {"good-1", "good-2"}


def test_main_never_crashes_on_adversarial_json_shapes():
    import subprocess

    adversarial_payloads = [
        "not even json {{{",
        "null",  # valid JSON, wrong top-level shape — not a dict
        "[]",
        json.dumps({"tasks": "not-a-list"}),
        json.dumps(
            {"tasks": [{"id": "t1"}]}
        ),  # minimal valid task, everything else absent
    ]
    for raw in adversarial_payloads:
        result = subprocess.run(
            [sys.executable, str(HOOKS / "subagent_statusline.py")],
            input=raw,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        assert result.returncode == 0, (
            f"subagent_statusline.py exited {result.returncode} on input {raw!r}\n{result.stderr}"
        )
