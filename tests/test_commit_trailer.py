"""Tests for commit_trailer.py — the commit attribution trailer read from the session transcript.

The transcript lines below are real `attachment` entries of type `model` (Sonnet, z.ai GLM,
Ollama Cloud Gemma), trimmed to the fields the script reads.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = (
    Path(__file__).parent.parent
    / "plugins"
    / "pc-git"
    / "hooks"
    / "branch-policy"
    / "commit_trailer.py"
)
SID = "40a363b2-34d4-41b8-8f08-b59d61a1147b"
NEUTRAL = "Co-Authored-By: Claude <noreply@anthropic.com>"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("commit_trailer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ct = _load()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_BRIDGE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _attachment(model_id: str, marketing: str | None, session: str = SID) -> str:
    return json.dumps(
        {
            "isSidechain": False,
            "attachment": {
                "type": "model",
                "identity": {
                    "modelId": model_id,
                    "marketingName": marketing,
                    "knowledgeCutoff": None,
                },
            },
            "type": "attachment",
            "sessionId": session,
        },
        separators=(",", ":"),
    )


def _assistant(model_id: str, session: str = SID, sidechain: bool = False) -> str:
    return json.dumps(
        {
            "isSidechain": sidechain,
            "type": "assistant",
            "message": {"model": model_id, "role": "assistant", "content": "x"},
            "sessionId": session,
        },
        separators=(",", ":"),
    )


def _transcript(tmp_path: Path, *lines: str, session: str = SID) -> Path:
    project = tmp_path / "projects" / "-tmp-project"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestShortModel:
    @pytest.mark.parametrize(
        ("model_id", "expected"),
        [
            ("claude-opus-4-8", "Opus 4.8"),
            ("claude-sonnet-5", "Sonnet 5"),
            ("claude-sonnet-5[1m]", "Sonnet 5"),
            ("claude-haiku-4-5-20251001", "Haiku 4.5"),
            ("us.anthropic.claude-sonnet-5-v1:0", "Sonnet 5"),
            ("", "?"),
        ],
    )
    def test_names(self, model_id: str, expected: str) -> None:
        assert ct.short_model(model_id) == expected

    def test_strip_suffix_only_removes_the_trailing_bracket(self) -> None:
        assert ct.strip_suffix("glm-5.3[1m]") == "glm-5.3"
        assert ct.strip_suffix("gemma4:31b-cloud[1m]") == "gemma4:31b-cloud"
        assert ct.strip_suffix("glm-5.3") == "glm-5.3"


class TestBuildTrailer:
    def test_anthropic_model_uses_marketing_name(self) -> None:
        identity = ct.ModelIdentity("claude-sonnet-5[1m]", "Sonnet 5")
        assert (
            ct.build_trailer(identity, None)
            == "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
        )

    def test_anthropic_model_without_marketing_name_uses_the_id(self) -> None:
        identity = ct.ModelIdentity("claude-opus-4-8")
        assert (
            ct.build_trailer(identity, None)
            == "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
        )

    def test_marketing_name_already_prefixed_is_not_doubled(self) -> None:
        identity = ct.ModelIdentity("claude-sonnet-5", "Claude Sonnet 5")
        assert (
            ct.build_trailer(identity, None)
            == "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
        )

    @pytest.mark.parametrize(
        ("model_id", "named"),
        [("glm-5.3[1m]", "glm-5.3"), ("gemma4:31b-cloud", "gemma4:31b-cloud")],
    )
    def test_other_model_is_named_behind_the_harness(
        self, model_id: str, named: str
    ) -> None:
        expected = f"Co-Authored-By: Claude Code ({named}) <noreply@anthropic.com>"
        assert ct.build_trailer(ct.ModelIdentity(model_id), None) == expected

    def test_no_identity_is_the_neutral_line(self) -> None:
        assert ct.build_trailer(None, None) == NEUTRAL

    def test_session_line_only_with_a_non_empty_bridge_id(self) -> None:
        with_id = ct.build_trailer(None, "session_X")
        assert with_id == f"{NEUTRAL}\nClaude-Session: https://claude.ai/code/session_X"
        assert ct.build_trailer(None, "") == NEUTRAL
        assert ct.build_trailer(None, None) == NEUTRAL


class TestReadModelIdentity:
    def test_reads_the_attachment(self, tmp_path: Path) -> None:
        path = _transcript(tmp_path, _attachment("claude-sonnet-5[1m]", "Sonnet 5"))
        identity = ct.read_model_identity(path, SID)
        assert identity == ct.ModelIdentity("claude-sonnet-5[1m]", "Sonnet 5")

    def test_null_marketing_name_is_none(self, tmp_path: Path) -> None:
        path = _transcript(tmp_path, _attachment("glm-5.3[1m]", None))
        assert ct.read_model_identity(path, SID) == ct.ModelIdentity("glm-5.3[1m]")

    def test_last_attachment_wins(self, tmp_path: Path) -> None:
        path = _transcript(
            tmp_path,
            _attachment("glm-5.3[1m]", None),
            _assistant("glm-5.3"),
            _attachment("claude-opus-4-8", "Opus 4.8"),
        )
        identity = ct.read_model_identity(path, SID)
        assert identity is not None
        assert identity.model_id == "claude-opus-4-8"

    def test_truncated_last_line_is_skipped(self, tmp_path: Path) -> None:
        path = _transcript(
            tmp_path,
            _attachment("glm-5.3[1m]", None),
            _attachment("claude-opus-4-8", "Opus 4.8")[:60],
        )
        identity = ct.read_model_identity(path, SID)
        assert identity is not None
        assert identity.model_id == "glm-5.3[1m]"

    def test_attachment_of_another_session_is_ignored(self, tmp_path: Path) -> None:
        path = _transcript(tmp_path, _attachment("glm-5.3[1m]", None, "other-session"))
        assert ct.read_model_identity(path, SID) is None

    def test_falls_back_to_the_last_assistant_model(self, tmp_path: Path) -> None:
        path = _transcript(
            tmp_path, _assistant("claude-haiku-4-5"), _assistant("claude-opus-4-8")
        )
        identity = ct.read_model_identity(path, SID)
        assert identity is not None
        assert identity.model_id == "claude-opus-4-8"

    def test_fallback_skips_synthetic_and_sidechain_lines(self, tmp_path: Path) -> None:
        path = _transcript(
            tmp_path,
            _assistant("claude-opus-4-8"),
            _assistant("<synthetic>"),
            _assistant("claude-haiku-4-5", sidechain=True),
        )
        identity = ct.read_model_identity(path, SID)
        assert identity is not None
        assert identity.model_id == "claude-opus-4-8"

    def test_attachment_beats_assistant_messages(self, tmp_path: Path) -> None:
        path = _transcript(
            tmp_path, _attachment("glm-5.3[1m]", None), _assistant("glm-5.3-flash")
        )
        identity = ct.read_model_identity(path, SID)
        assert identity is not None
        assert identity.model_id == "glm-5.3[1m]"

    def test_unreadable_file_is_none(self, tmp_path: Path) -> None:
        assert ct.read_model_identity(tmp_path / "missing.jsonl", SID) is None


class TestFindSessionFile:
    def test_finds_the_transcript_whatever_the_project_directory(
        self, tmp_path: Path
    ) -> None:
        path = _transcript(tmp_path, _attachment("glm-5.3[1m]", None))
        assert ct.find_session_file(SID, tmp_path) == path

    @pytest.mark.parametrize(
        "bad_id", ["", "${CLAUDE_SESSION_ID}", "../x", "*", "a/b", "a b"]
    )
    def test_refuses_anything_but_a_plain_identifier(
        self, tmp_path: Path, bad_id: str
    ) -> None:
        _transcript(tmp_path, _attachment("glm-5.3[1m]", None))
        assert ct.find_session_file(bad_id, tmp_path) is None

    def test_absent_transcript_is_none(self, tmp_path: Path) -> None:
        assert ct.find_session_file(SID, tmp_path) is None


class TestMain:
    def test_end_to_end_non_anthropic(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _transcript(tmp_path, _attachment("glm-5.3[1m]", None))
        code = ct.main(["--session-id", SID, "--config-dir", str(tmp_path)])
        assert code == 0
        assert capsys.readouterr().out.splitlines() == [
            "Co-Authored-By: Claude Code (glm-5.3) <noreply@anthropic.com>"
        ]

    def test_config_dir_comes_from_the_environment(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _transcript(tmp_path, _attachment("claude-sonnet-5[1m]", "Sonnet 5"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_B")
        assert ct.main(["--session-id", SID]) == 0
        assert capsys.readouterr().out.splitlines() == [
            "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>",
            "Claude-Session: https://claude.ai/code/session_B",
        ]

    @pytest.mark.parametrize("session_id", ["", "${CLAUDE_SESSION_ID}", "unknown-id"])
    def test_unusable_session_id_degrades_to_the_neutral_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], session_id: str
    ) -> None:
        _transcript(tmp_path, _attachment("glm-5.3[1m]", None))
        code = ct.main(["--session-id", session_id, "--config-dir", str(tmp_path)])
        assert code == 0
        assert capsys.readouterr().out.splitlines() == [NEUTRAL]
