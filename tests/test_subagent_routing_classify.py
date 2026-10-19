"""Tests for the subagent-routing classifier entrypoint."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fake_local_server import NON_LOOPBACK_URLS, FakeServer, RawServer

ROUTING = Path(__file__).parent.parent / "plugins/pc-core/hooks/subagent-routing"
_spec = importlib.util.spec_from_file_location(
    "subagent_routing_classify", ROUTING / "classify.py"
)
assert _spec and _spec.loader
classify_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = classify_mod
_spec.loader.exec_module(classify_mod)
_jspec = importlib.util.spec_from_file_location("journal", ROUTING / "journal.py")
assert _jspec and _jspec.loader
journal_mod = importlib.util.module_from_spec(_jspec)
_jspec.loader.exec_module(journal_mod)

PROBS = {"routine": 0.01, "logic": 0.04, "hard": 0.95}


@pytest.fixture
def plugin_grid() -> dict:
    return json.loads((ROUTING / "grid.json").read_text())


def write_settings(root: Path, data: dict) -> None:
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "settings.pinky.json").write_text(json.dumps(data))


def test_plugin_grid_is_valid(plugin_grid):
    assert classify_mod.valid_grid(plugin_grid)


@pytest.fixture
def server():
    fake = FakeServer()
    yield fake
    fake.close()


def answer(choice: str) -> dict:
    return {"answers": {"cat": {"choice": choice, "probabilities": PROBS}}}


@pytest.mark.parametrize(
    ("choice", "model", "effort"),
    [
        ("routine", "haiku", "low"),
        ("logic", "sonnet", "medium"),
        ("hard", "opus", "high"),
    ],
)
def test_choice_parsed_to_model_effort_and_probabilities(
    server, plugin_grid, choice, model, effort
):
    server.answer(200, answer(choice))
    result = classify_mod.classify("write tests", "m1", plugin_grid, server.url)
    assert result == {
        "category": choice,
        "model": model,
        "effort": effort,
        "model_id": "m1",
        "probabilities": PROBS,
    }


def test_request_is_the_scored_one_with_full_text(server, plugin_grid):
    server.answer(200, answer("logic"))
    task = "x" * 5000
    classify_mod.classify(task, "m1", plugin_grid, server.url + "/")
    assert server.requests == [
        {
            "path": "/v1/systemone",
            "json": {
                "model": "m1",
                "state": task,
                "questions": {
                    "cat": {
                        "type": "choice",
                        "instructions": (
                            "Which category of this grid fits the coding task described "
                            "in the state, judging only how demanding the task itself is?"
                        ),
                        "criteria": {
                            name: cat["description"]
                            for name, cat in plugin_grid["categories"].items()
                        },
                    }
                },
                "truncate": False,
            },
        }
    ]


def test_http_507_gives_default_and_error(server, plugin_grid):
    server.answer(507, {"error": {"message": "not enough memory"}})
    result = classify_mod.classify("t", "m1", plugin_grid, server.url)
    assert result == {
        "category": "logic",
        "model": "sonnet",
        "effort": "medium",
        "model_id": "m1",
        "error": "HTTP 507",
    }


@pytest.mark.parametrize(
    "payload",
    [{}, {"answers": {"cat": {"choice": "routine"}}}, answer("legendary"), b"not json"],
)
def test_unparsable_or_out_of_grid_answer_gives_default_and_error(
    server, plugin_grid, payload
):
    server.answer(200, payload)
    result = classify_mod.classify("t", "m1", plugin_grid, server.url)
    assert result["category"] == plugin_grid["default"]
    assert "error" in result
    assert "probabilities" not in result


def test_server_down_gives_default_and_error(plugin_grid):
    fake = FakeServer()
    url = fake.url
    fake.close()
    result = classify_mod.classify("t", "m1", plugin_grid, url)
    assert result["category"] == plugin_grid["default"]
    assert "error" in result


def test_redirect_refused(plugin_grid):
    raw = RawServer(
        b"HTTP/1.1 307 Temporary Redirect\r\nLocation: http://example.com/\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    result = classify_mod.classify("t", "m1", plugin_grid, raw.url)
    raw.close()
    assert result["error"] == "HTTP 307"


@pytest.mark.parametrize("url", NON_LOOPBACK_URLS)
def test_non_loopback_url_refused_without_request(plugin_grid, url):
    raw = RawServer(b"")
    result = classify_mod.classify("t", "m1", plugin_grid, url)
    raw.close()
    assert "error" in result
    assert raw.connections == 0


def run_cli(tmp_path: Path, env: dict[str, str], *args: str):
    base = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ONE_DECISION", "LOCAL_INFERENCE"))
    }
    return subprocess.run(
        [sys.executable, str(ROUTING / "classify.py"), *args],
        input="refactor three modules",
        capture_output=True,
        text=True,
        env={**base, "CLAUDE_PROJECT_DIR": str(tmp_path), **env},
        check=False,
    )


@pytest.mark.parametrize(
    ("env", "args", "expected"),
    [
        ({}, [], "clef-flash-4bit"),
        ({"ONE_DECISION_MODEL": "from-env"}, [], "from-env"),
        ({"ONE_DECISION_MODEL": "from-env"}, ["--model", "from-flag"], "from-flag"),
    ],
)
def test_model_resolution_order(tmp_path, server, env, args, expected):
    server.answer(200, answer("routine"))
    out = run_cli(tmp_path, {"LOCAL_INFERENCE_BASE_URL": server.url, **env}, *args)
    assert out.returncode == 0
    assert json.loads(out.stdout)["category"] == "routine"
    assert server.requests[0]["json"]["model"] == expected


def test_cli_server_down_prints_default_exit_0(tmp_path):
    fake = FakeServer()
    url = fake.url
    fake.close()
    out = run_cli(tmp_path, {"LOCAL_INFERENCE_BASE_URL": url})
    assert out.returncode == 0
    line = json.loads(out.stdout)
    assert line["category"] == "logic"
    assert "error" in line


def test_cli_non_loopback_url_prints_default_exit_0(tmp_path):
    out = run_cli(tmp_path, {"LOCAL_INFERENCE_BASE_URL": "http://example.com"})
    assert out.returncode == 0
    assert "error" in json.loads(out.stdout)


def test_no_timeout_by_default_and_opt_in(monkeypatch, plugin_grid):
    seen: list[int | None] = []

    def fake_post(url, body, timeout):
        seen.append(timeout)
        return answer("logic")

    monkeypatch.setattr(classify_mod, "post", fake_post)
    classify_mod.classify("t", "m", plugin_grid, "http://127.0.0.1:1")
    classify_mod.classify("t", "m", plugin_grid, "http://127.0.0.1:1", timeout=7)
    assert seen == [None, 7]


def test_timeout_gives_default_and_error(plugin_grid):
    raw = RawServer()
    result = classify_mod.classify("t", "m1", plugin_grid, raw.url, timeout=1)
    raw.close()
    assert result["category"] == plugin_grid["default"]
    assert "error" in result


DECISION = {
    "prompt": "p" * 500,
    "agent_type": "general-purpose",
    "requested_model": "opus",
    "requested_effort": "high",
    "category": "routine",
    "applied_model": "haiku",
    "applied_effort": "low",
    "classifier_model": "clef-flash-4bit",
    "error": "",
    "reason": "classified",
    "api_key": "s3cret",  # pragma: allowlist secret
}


def run_journal(tmp_path: Path, decision: dict):
    return subprocess.run(
        [sys.executable, str(ROUTING / "journal.py")],
        input=json.dumps(decision),
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path)},
        check=False,
    )


def test_journal_line_has_the_fields_and_truncated_prompt(tmp_path):
    out = run_journal(tmp_path, DECISION)
    assert out.returncode == 0
    lines = (
        (tmp_path / ".claude/pc-core/subagent-routing.jsonl").read_text().splitlines()
    )
    assert len(lines) == 1
    entry = json.loads(lines[0])
    ts = entry.pop("ts")
    assert ts.endswith("+00:00")
    assert entry == {
        **{k: v for k, v in DECISION.items() if k != "api_key"},
        "prompt": "p" * 200,
    }
    assert "s3cret" not in lines[0]


def test_journal_keeps_the_exit_reason_and_the_classifier_error(tmp_path):
    decision = {**DECISION, "reason": "no-band", "error": "HTTP 507"}
    assert run_journal(tmp_path, decision).returncode == 0
    path = tmp_path / ".claude/pc-core/subagent-routing.jsonl"
    entry = json.loads(path.read_text())
    assert (entry["reason"], entry["error"]) == ("no-band", "HTTP 507")


def test_journal_appends_one_line_per_decision(tmp_path):
    run_journal(tmp_path, DECISION)
    run_journal(tmp_path, {**DECISION, "reason": "timeout"})
    path = tmp_path / ".claude/pc-core/subagent-routing.jsonl"
    reasons = [json.loads(line)["reason"] for line in path.read_text().splitlines()]
    assert reasons == ["classified", "timeout"]


@pytest.mark.parametrize("reason", ["classified", "timeout", "no-band", "user-choice"])
def test_journal_accepts_every_reason(tmp_path, reason):
    assert run_journal(tmp_path, {**DECISION, "reason": reason}).returncode == 0


@pytest.mark.parametrize(
    "reason", ["", "oops", "errors", "error:HTTP 507", "classified\nx"]
)
def test_journal_rejects_unknown_reason_and_writes_nothing(tmp_path, reason):
    out = run_journal(tmp_path, {**DECISION, "reason": reason})
    assert out.returncode == 2
    assert not (tmp_path / ".claude/pc-core/subagent-routing.jsonl").exists()


def test_journal_line_is_capped_and_single_line(tmp_path):
    big = {k: '\n"\\' * 500 for k in DECISION} | {"reason": "timeout"}
    assert run_journal(tmp_path, big).returncode == 0
    text = (tmp_path / ".claude/pc-core/subagent-routing.jsonl").read_text()
    assert len(text.splitlines()) == 1
    assert len(text) < 4000


def test_journal_unreadable_input_exits_2(tmp_path):
    out = subprocess.run(
        [sys.executable, str(ROUTING / "journal.py")],
        input="not json",
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path)},
        check=False,
    )
    assert out.returncode == 2


def test_repo_override_honoured(tmp_path):
    grid = {
        "default": "cheap",
        "categories": {
            "cheap": {"model": "haiku", "effort": "low", "description": "easy"},
            "pricey": {"model": "opus", "effort": "medium", "description": "hard"},
        },
    }
    write_settings(tmp_path, {"subagent_routing": {"grid": grid}})
    loaded = classify_mod.load_grid(tmp_path)
    assert loaded == grid
    assert loaded["categories"]["pricey"]["model"] == "opus"


@pytest.mark.parametrize(
    "bad",
    [
        {"default": "x", "categories": {}},
        {
            "default": "missing",
            "categories": {
                "a": {"model": "haiku", "effort": "low", "description": "d"}
            },
        },
        {
            "default": "a",
            "categories": {"a": {"model": "gpt", "effort": "low", "description": "d"}},
        },
        {
            "default": "a",
            "categories": {
                "a": {"model": "haiku", "effort": "max", "description": "d"}
            },
        },
        "not a grid",
    ],
)
def test_invalid_override_falls_back_to_plugin_grid(tmp_path, plugin_grid, bad):
    write_settings(tmp_path, {"subagent_routing": {"grid": bad}})
    assert classify_mod.load_grid(tmp_path) == plugin_grid


def test_no_override_uses_plugin_grid(tmp_path, plugin_grid):
    assert classify_mod.load_grid(tmp_path) == plugin_grid


def _models_json(folder: Path, base_url: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "models.json").write_text(
        json.dumps({"providers": {"mlx": {"baseUrl": base_url}}})
    )


def test_resolve_returns_the_variable(monkeypatch):
    monkeypatch.setenv("LOCAL_INFERENCE_BASE_URL", "http://127.0.0.1:2")
    assert classify_mod.resolve_base_url() == "http://127.0.0.1:2"


@pytest.mark.parametrize("value", [None, ""])
def test_resolve_unset_or_empty_is_none_even_with_a_models_json(
    tmp_path, monkeypatch, value
):
    _models_json(tmp_path / ".pi" / "agent", "http://localhost:1/v1")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / ".pi" / "agent"))
    monkeypatch.setenv("HOME", str(tmp_path))
    if value is None:
        monkeypatch.delenv("LOCAL_INFERENCE_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("LOCAL_INFERENCE_BASE_URL", value)
    assert classify_mod.resolve_base_url() is None


def test_classify_without_an_address_prints_default_with_error(plugin_grid):
    result = classify_mod.classify("t", "m", plugin_grid, None)
    assert result["category"] == plugin_grid["default"]
    assert "error" in result


@pytest.mark.parametrize("value", [None, ""])
def test_cli_without_an_address_prints_default_exit_0_despite_a_models_json(
    tmp_path, server, value
):
    server.answer(200, answer("routine"))
    pi_dir = tmp_path / "pi"
    _models_json(pi_dir, server.url + "/v1")
    env = {"PI_CODING_AGENT_DIR": str(pi_dir), "HOME": str(tmp_path)}
    if value is not None:
        env["LOCAL_INFERENCE_BASE_URL"] = value
    out = run_cli(tmp_path, env)
    assert out.returncode == 0
    line = json.loads(out.stdout)
    assert line["category"] == "logic"
    assert "error" in line
    assert server.requests == []
