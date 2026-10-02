"""Tests for the pc-memory PreToolUse write guard — run with: uv run pytest tests/test_pc_memory_write.py"""

import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parent.parent / "plugins" / "pc-memory"
HOOK = PLUGIN / "hooks" / "memory-write" / "memory_write_guard.py"
sys.path.insert(0, str(HOOK.parent))

import memory_write_guard as mw  # noqa: E402
from pc_memory_lib import encode  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No ambient setting leaks into a test, nor into the subprocess."""
    for name in ("CLAUDE_CONFIG_DIR", "PC_ALLOW_MEMORY_WRITE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Fake home with a `.claude/projects`, set as `HOME`."""
    root = tmp_path.resolve()
    (root / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(root))
    return root


@pytest.fixture
def repo(home):
    """home/ws/repo, a git root."""
    path = home / "ws" / "repo"
    (path / ".git").mkdir(parents=True)
    return path


def _payload(tool: str, cwd: Path, **tool_input: str) -> dict:
    return {"tool_name": tool, "cwd": str(cwd), "tool_input": tool_input}


def _decision(output: dict) -> str | None:
    return output.get("hookSpecificOutput", {}).get("permissionDecision")


# --- which writes ask ---------------------------------------------------------------


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_default_silo_file_write_asks(home, repo, tool):
    silo = mw.memory_dir_of(repo)
    out = mw.build_output(_payload(tool, repo, file_path=str(silo / "feedback_x.md")))
    assert _decision(out) == "ask"
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_silo_that_does_not_exist_yet_asks(home, repo):
    silo = mw.memory_dir_of(repo)
    assert not silo.exists()
    out = mw.build_output(_payload("Write", repo, file_path=str(silo / "MEMORY.md")))
    assert _decision(out) == "ask"


def test_declared_directory_in_level_settings_asks(home, repo):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": "notes/mem"}), encoding="utf-8"
    )
    out = mw.build_output(
        _payload("Write", repo, file_path=str(repo / "notes" / "mem" / "a.md"))
    )
    assert _decision(out) == "ask"


def test_declared_directory_in_user_settings_asks(home, repo):
    target = home / "elsewhere" / "mem"
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": str(target)}), encoding="utf-8"
    )
    out = mw.build_output(_payload("Edit", repo, file_path=str(target / "a.md")))
    assert _decision(out) == "ask"


def test_default_silo_still_asks_when_a_directory_is_declared(home, repo):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": "notes/mem"}), encoding="utf-8"
    )
    default = home / ".claude" / "projects" / encode(repo) / "memory"
    declared = repo / "notes" / "mem"
    for target in (default, declared):
        out = mw.build_output(_payload("Write", repo, file_path=str(target / "a.md")))
        assert _decision(out) == "ask"


def test_relative_path_resolves_from_cwd(home, repo):
    silo = mw.memory_dir_of(repo)
    silo.mkdir(parents=True)
    out = mw.build_output(_payload("Write", silo, file_path="note.md"))
    assert _decision(out) == "ask"


def test_dot_dot_traversal_into_memory_asks(home, repo):
    silo = mw.memory_dir_of(repo)
    out = mw.build_output(
        _payload(
            "Write", repo, file_path=str(silo.parent / "x" / ".." / "memory" / "a.md")
        )
    )
    assert _decision(out) == "ask"


def test_silo_of_another_project_asks(home, repo):
    other = home / ".claude" / "projects" / "-some-other-project" / "memory" / "a.md"
    assert (
        _decision(mw.build_output(_payload("Write", repo, file_path=str(other))))
        == "ask"
    )


def test_parent_level_silo_asks(home, repo):
    parent = repo.parent
    out = mw.build_output(
        _payload("Write", repo, file_path=str(mw.memory_dir_of(parent) / "a.md"))
    )
    assert _decision(out) == "ask"


def test_bash_write_into_silo_asks(home, repo):
    silo = mw.memory_dir_of(repo)
    out = mw.build_output(
        _payload("Bash", repo, command=f"echo note > {silo}/feedback_x.md")
    )
    assert _decision(out) == "ask"


@pytest.mark.parametrize(
    "template",
    [
        "tee {d}/a.md <<< x",
        "sed -i 's/a/b/' {d}/a.md",
        "sed -ni 's/a/b/p' {d}/a.md",
        "mv /tmp/a.md {d}/a.md",
        "cp /tmp/a.md {d}/",
        "rm {d}/a.md",
        "touch {d}/a.md",
        "echo x >> '{d}/a.md'",
        "echo x > ~/{h}/a.md",
        'echo x > "$HOME/{h}/a.md"',
    ],
)
def test_bash_write_forms_ask(home, repo, template):
    silo = mw.memory_dir_of(repo)
    command = template.format(d=silo, h=silo.relative_to(home))
    assert _decision(mw.build_output(_payload("Bash", repo, command=command))) == "ask"


def test_bash_write_into_declared_directory_asks(home, repo):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text(
        json.dumps({"autoMemoryDirectory": str(home / "mem")}), encoding="utf-8"
    )
    out = mw.build_output(_payload("Bash", repo, command=f"touch {home}/mem/a.md"))
    assert _decision(out) == "ask"


# --- no decision ---------------------------------------------------------------------------------


def test_path_outside_memory_has_no_decision(home, repo):
    assert mw.build_output(_payload("Write", repo, file_path=str(repo / "a.md"))) == {}


def test_sibling_directory_with_memory_prefix_has_no_decision(home, repo):
    silo = mw.memory_dir_of(repo)
    sibling = silo.parent / "memory-backup" / "a.md"
    assert mw.build_output(_payload("Write", repo, file_path=str(sibling))) == {}
    command = f"touch {silo}-backup/a.md"
    assert mw.build_output(_payload("Bash", repo, command=command)) == {}


def test_bash_read_of_memory_has_no_decision(home, repo):
    silo = mw.memory_dir_of(repo)
    for command in (
        f"cat {silo}/MEMORY.md",
        f"ls {silo} 2>&1",
        f"grep -r x {silo} 2>/dev/null",
    ):
        assert mw.build_output(_payload("Bash", repo, command=command)) == {}


def test_bash_write_outside_memory_has_no_decision(home, repo):
    assert mw.build_output(_payload("Bash", repo, command="echo x > out.txt")) == {}


def test_other_tools_have_no_decision(home, repo):
    silo = mw.memory_dir_of(repo)
    assert mw.build_output(_payload("Read", repo, file_path=str(silo / "a.md"))) == {}


def test_malformed_input_has_no_decision(home, repo):
    assert mw.build_output({"tool_name": "Write", "tool_input": "x"}) == {}
    assert mw.build_output({"tool_name": "Bash", "tool_input": {"command": 3}}) == {}
    assert mw.build_output({}) == {}


# --- the reason ---------------------------------------------------------------------


def test_reason_covers_the_three_cases_in_plain_text(home, repo):
    out = mw.build_output(
        _payload("Write", repo, file_path=str(mw.memory_dir_of(repo) / "a.md"))
    )
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "SKILL" in reason  # skipped step -> diff of the SKILL
    assert "CLAUDE.md" in reason  # general rule -> promotion
    assert "local trap" in reason  # local trap -> a note is acceptable
    assert reason.isascii()
    assert not any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in reason)


def test_output_is_exactly_the_ask_decision(home, repo):
    out = mw.build_output(
        _payload("Write", repo, file_path=str(mw.memory_dir_of(repo) / "a.md"))
    )
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": (
                "Write into memory. A step of a procedure that was skipped: fix the SKILL "
                "concerned (a diff of that SKILL), not a note. A general rule: promote it to a "
                "CLAUDE.md or a rule at the right level. A local trap: a note is acceptable. "
                "Allow only if a note is the right place."
            ),
        }
    }


# --- opt-out ------------------------------------------------------------------------


def _main(payload: dict, home: Path, **env: str) -> subprocess.CompletedProcess:
    full = {**os.environ, "HOME": str(home), **env}
    full.pop("CLAUDE_CONFIG_DIR", None)
    if "PC_ALLOW_MEMORY_WRITE" not in env:
        full.pop("PC_ALLOW_MEMORY_WRITE", None)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=full,
        check=False,
    )


def test_main_prints_the_decision(home, repo):
    payload = _payload("Write", repo, file_path=str(mw.memory_dir_of(repo) / "a.md"))
    result = _main(payload, home)
    assert result.returncode == 0
    assert _decision(json.loads(result.stdout)) == "ask"


def test_allow_variable_lets_the_write_through(home, repo):
    payload = _payload("Write", repo, file_path=str(mw.memory_dir_of(repo) / "a.md"))
    result = _main(payload, home, PC_ALLOW_MEMORY_WRITE="1")
    assert result.returncode == 0
    assert result.stdout == ""


def test_allow_variable_other_than_one_still_asks(home, repo):
    payload = _payload("Write", repo, file_path=str(mw.memory_dir_of(repo) / "a.md"))
    result = _main(payload, home, PC_ALLOW_MEMORY_WRITE="0")
    assert _decision(json.loads(result.stdout)) == "ask"


def test_main_never_fails_on_bad_input(home):
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""


# --- Bash false positives: no decision --------------------------------------------------------


def _bash(repo: Path, command: str) -> dict:
    return mw.build_output(_payload("Bash", repo, command=command))


@pytest.mark.parametrize(
    "suffix", [">/dev/null", "> /dev/null", "&>/dev/null", "2>&1", "2>/dev/null"]
)
def test_bash_noop_redirection_is_not_a_write(home, repo, suffix):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, f"cat {silo}/MEMORY.md {suffix}") == {}


def test_bash_redirection_to_memory_after_a_noop_one_still_asks(home, repo):
    silo = mw.memory_dir_of(repo)
    command = f"echo x 2>/dev/null > {silo}/a.md"
    assert _decision(_bash(repo, command)) == "ask"


@pytest.mark.parametrize("verb", ["cp", "mv"])
def test_bash_memory_as_source_is_not_a_write(home, repo, verb):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, f"{verb} {silo}/a.md /tmp/a.md") == {}


@pytest.mark.parametrize("verb", ["cp", "mv"])
def test_bash_memory_as_destination_asks(home, repo, verb):
    silo = mw.memory_dir_of(repo)
    assert _decision(_bash(repo, f"{verb} /tmp/a.md {silo}/a.md")) == "ask"


def test_bash_memory_as_redirection_source_is_not_a_write(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, f"cat {silo}/x > /tmp/y") == {}


@pytest.mark.parametrize("name", ["install.md", "rm-notes.md", "touch.md", "tee.md"])
def test_bash_verb_in_a_file_name_is_not_a_write(home, repo, name):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, f"cat {silo}/{name}") == {}
    assert _bash(repo, f"grep -l x {silo}/{name}") == {}


def test_bash_file_named_like_a_verb_is_still_a_redirection_target(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _decision(_bash(repo, f"echo x > {silo}/install.md")) == "ask"


def test_bash_commit_message_quoting_a_memory_path_is_not_a_write(home, repo):
    silo = mw.memory_dir_of(repo)
    command = f'git commit -m "remove stale note {silo}/a.md"'
    assert _bash(repo, command) == {}


# --- Bash verbs in command position ---------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "sed --in-place 's/a/b/' {d}/a.md",
        "sed -i.bak 's/a/b/' {d}/a.md",
        "perl -i -e 's/a/b/' {d}/a.md",
        "perl -pi -e 's/a/b/' {d}/a.md",
        "rsync -a /tmp/a.md {d}/",
        "curl -o {d}/a.md https://example.com/a",
        "curl --output {d}/a.md https://example.com/a",
        "wget -O {d}/a.md https://example.com/a",
        "dd if=/tmp/a of={d}/a.md",
        "ln -s /tmp/a.md {d}/a.md",
        "install -m 644 /tmp/a.md {d}/a.md",
        "truncate -s 0 {d}/a.md",
        "cd /tmp && rm {d}/a.md",
        "ls; touch {d}/a.md",
        "ls | tee {d}/a.md",
        "(rm {d}/a.md)",
        "FOO=1 rm {d}/a.md",
        "sudo rm {d}/a.md",
        "/bin/rm {d}/a.md",
        'rm "$HOME"/{h}/a.md',
        "rm ${{HOME}}/{h}/a.md",
        "rm {d}/x/../a.md",
    ],
)
def test_bash_cheap_bypass_forms_ask(home, repo, template):
    silo = mw.memory_dir_of(repo)
    command = template.format(d=silo, h=silo.relative_to(home))
    assert _decision(_bash(repo, command)) == "ask"


def test_bash_curl_and_wget_without_output_flag_are_not_writes(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, f"curl https://example.com/{silo.name}") == {}
    assert _bash(repo, f"wget -q -O /tmp/a {silo}") == {}


def test_bash_sed_without_in_place_is_not_a_write(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, f"sed -n 's/a/b/p' {silo}/a.md") == {}


def test_bash_relative_path_resolves_from_cwd(home, repo):
    silo = mw.memory_dir_of(repo)
    silo.mkdir(parents=True)
    out = mw.build_output(_payload("Bash", silo, command="touch a.md"))
    assert _decision(out) == "ask"


def test_bash_relative_path_outside_memory_has_no_decision(home, repo):
    assert _bash(repo, "touch a.md") == {}


# --- paths: symlinks and relative spellings --------------------------------------------------


def test_declared_directory_that_is_a_symlink_asks_through_both_names(home, repo):
    real = home / "real-mem"
    real.mkdir()
    link = home / "link-mem"
    link.symlink_to(real)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": str(link)}), encoding="utf-8"
    )
    for name in (link, real):
        out = mw.build_output(_payload("Write", repo, file_path=str(name / "a.md")))
        assert _decision(out) == "ask"
        assert _decision(_bash(repo, f"touch {name}/a.md")) == "ask"


def test_file_tool_relative_path_from_another_cwd_asks(home, repo):
    silo = mw.memory_dir_of(repo)
    relative = os.path.relpath(silo / "a.md", repo)
    out = mw.build_output(_payload("Write", repo, file_path=relative))
    assert _decision(out) == "ask"


def test_bash_dot_dot_into_memory_asks(home, repo):
    silo = mw.memory_dir_of(repo)
    command = f"touch {silo.parent}/other/../memory/a.md"
    assert _decision(_bash(repo, command)) == "ask"


def test_bash_dot_dot_out_of_memory_has_no_decision(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, f"touch {silo}/../elsewhere.md") == {}


# --- Bash false positives: heredocs, quotes, comments, sources ---------------------------------


def test_bash_heredoc_body_is_not_looked_into(home, repo):
    silo = mw.memory_dir_of(repo)
    command = (
        "git commit -m \"$(cat <<'EOF'\nfix guard\n\n"
        f'rm {silo}/a.md was asked\ntouch {silo}/b.md too\nEOF\n)"'
    )
    assert _bash(repo, command) == {}


@pytest.mark.parametrize("opener", ["<<EOF", "<<'EOF'", '<<"EOF"', "<<-EOF"])
def test_bash_heredoc_forms_are_not_looked_into(home, repo, opener):
    silo = mw.memory_dir_of(repo)
    command = f"cat {opener}\nrm {silo}/a.md\nEOF"
    assert _bash(repo, command) == {}


def test_bash_heredoc_with_a_memory_target_still_asks(home, repo):
    silo = mw.memory_dir_of(repo)
    command = f"cat <<EOF > {silo}/a.md\nbody\nEOF"
    assert _decision(_bash(repo, command)) == "ask"


def test_bash_command_after_a_heredoc_is_still_read(home, repo):
    silo = mw.memory_dir_of(repo)
    command = f"cat <<EOF\nbody\nEOF\nrm {silo}/a.md"
    assert _decision(_bash(repo, command)) == "ask"


@pytest.mark.parametrize(
    "template",
    [
        'git commit -m "write output > {d}/a.md"',
        'echo "result > {d}/a.md"',
        'gh pr create --body "moves x to > {d}/a.md"',
        'git commit -m "fix; rm {d}/a.md"',
        'git commit -m "a | touch {d}/a.md"',
        "git commit -m 'a && rm {d}/a.md'",
        "echo 'x > {d}/a.md'",
    ],
)
def test_bash_quoted_text_is_not_a_write(home, repo, template):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, template.format(d=silo)) == {}


def test_bash_real_redirection_after_quoted_text_asks(home, repo):
    silo = mw.memory_dir_of(repo)
    command = f'echo "a > b" > {silo}/a.md'
    assert _decision(_bash(repo, command)) == "ask"


def test_bash_escaped_quote_does_not_end_the_quoted_text(home, repo):
    silo = mw.memory_dir_of(repo)
    command = f'echo "say \\" > {silo}/a.md"'
    assert _bash(repo, command) == {}


def test_bash_trailing_comment_is_ignored(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, f"rm -rf /tmp/x # {silo}") == {}
    assert _bash(repo, f"echo done # rm {silo}/a.md") == {}


def test_bash_hash_inside_a_word_is_not_a_comment(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _decision(_bash(repo, f"touch a#b {silo}/a.md")) == "ask"


def test_bash_redirection_source_is_not_a_target(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, f"tee /tmp/a < {silo}/a.md") == {}


def test_bash_touch_reference_is_not_a_target(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, f"touch -r {silo}/a.md /tmp/b") == {}
    assert _decision(_bash(repo, f"touch -r /tmp/b {silo}/a.md")) == "ask"


# --- Bash bypass forms -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "curl --output={d}/a https://example.com/a",
        "curl -o{d}/a https://example.com/a",
        "curl -sSo {d}/a https://example.com/a",
        "wget -qO {d}/a https://example.com/a",
        "wget -O{d}/a https://example.com/a",
        "wget --output-document={d}/a https://example.com/a",
        "wget --output-document {d}/a https://example.com/a",
        "wget -P {d} https://example.com/a",
        "wget --directory-prefix={d} https://example.com/a",
        "cp -t {d} /tmp/a /tmp/b",
        "mv -t {d} /tmp/a",
        "cp --target-directory={d} /tmp/a",
        "cp --target-directory {d} /tmp/a",
        "install -t {d} /tmp/a",
        "echo x >| {d}/a",
        "echo x &> {d}/a",
        "echo x >&{d}/a",
        "\\rm {d}/a",
        "'rm' {d}/a",
        '"rm" {d}/a',
        "sudo -u me rm {d}/a",
        "env -i rm {d}/a",
        "env FOO=1 rm {d}/a",
        "ls $(rm {d}/a)",
    ],
)
def test_bash_common_bypass_forms_ask(home, repo, template):
    silo = mw.memory_dir_of(repo)
    assert _decision(_bash(repo, template.format(d=silo))) == "ask"


def test_bash_target_directory_outside_memory_has_no_decision(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, f"cp -t /tmp {silo}/a.md") == {}
    assert _bash(repo, f"wget -P /tmp {silo}") == {}
