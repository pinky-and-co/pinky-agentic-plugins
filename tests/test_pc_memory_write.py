"""Tests for the pc-memory PreToolUse write guard — run with: uv run pytest tests/test_pc_memory_write.py"""

import io
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
    out = mw.build_output(
        _payload("Write", repo, file_path=str(silo / "feedback_x.md"))
    )
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


def test_bash_curl_and_wget_naming_memory_without_writing_it_have_no_decision(
    home, repo
):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, f"curl -o /tmp/a file://{silo}/a.md") == {}
    assert _bash(repo, f"curl https://example.com{silo}/a.md") == {}
    assert _bash(repo, f"wget -q -O /tmp/a {silo}/a.md") == {}
    assert _bash(repo, f"curl {silo}/a.md") == {}


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


# --- heredocs, here-strings, arithmetic ------------------------------------------------------


@pytest.mark.parametrize("operator", ["<<< foo", "<<<foo", '<<<"foo"'])
def test_bash_here_string_is_not_a_heredoc(home, repo, operator):
    silo = mw.memory_dir_of(repo)
    command = f"cat {operator}\nrm {silo}/a.md"
    assert _decision(_bash(repo, command)) == "ask"


@pytest.mark.parametrize(
    "prefix",
    [
        'git commit -m "explain <<EOF"',
        "echo 'a <<EOF'",
        "ls # <<EOF",
        "x=$((1<<n))",
        "(( x = 1 << n ))",
        'echo "$((1<<n))"',
    ],
)
def test_bash_heredoc_operator_outside_code_does_not_open_a_heredoc(home, repo, prefix):
    silo = mw.memory_dir_of(repo)
    assert _decision(_bash(repo, f"{prefix}\nrm {silo}/a.md")) == "ask"


@pytest.mark.parametrize(
    ("opener", "delimiter", "decoy"),
    [
        ("<<\\EOF", "EOF", "EOFX"),
        ("<<'END-OF'", "END-OF", "END"),
        ("<<EOF-1", "EOF-1", "EOF"),
        ('<<"EOF"', "EOF", "EO"),
    ],
)
def test_bash_heredoc_delimiter_forms(home, repo, opener, delimiter, decoy):
    silo = mw.memory_dir_of(repo)
    body = f"cat {opener}\n{decoy}\nrm {silo}/a.md\n{delimiter}"
    assert _bash(repo, body) == {}
    assert _decision(_bash(repo, f"{body}\nrm {silo}/b.md")) == "ask"


def test_bash_heredoc_with_crlf_line_endings(home, repo):
    silo = mw.memory_dir_of(repo)
    body = f"cat <<EOF\r\nrm {silo}/a.md\r\nEOF\r\n"
    assert _bash(repo, body) == {}
    assert _decision(_bash(repo, body + f"rm {silo}/b.md\r\n")) == "ask"


def test_bash_heredoc_body_starts_after_a_continued_line(home, repo):
    silo = mw.memory_dir_of(repo)
    body = f"cat <<EOF \\\n> /tmp/x\nrm {silo}/a.md\nEOF"
    assert _bash(repo, body) == {}
    assert _decision(_bash(repo, f"{body}\ntouch {silo}/b.md")) == "ask"


def test_bash_continued_line_can_redirect_into_memory(home, repo):
    silo = mw.memory_dir_of(repo)
    command = f"cat <<EOF \\\n> {silo}/a.md\nbody\nEOF"
    assert _decision(_bash(repo, command)) == "ask"


def test_bash_two_heredocs_on_one_line(home, repo):
    silo = mw.memory_dir_of(repo)
    body = f"cat <<A <<B\nrm {silo}/x\nA\ntouch {silo}/y\nB"
    assert _bash(repo, body) == {}
    assert _decision(_bash(repo, f"{body}\nrm {silo}/z")) == "ask"


def test_bash_dash_heredoc_with_tab_indented_terminator(home, repo):
    silo = mw.memory_dir_of(repo)
    body = f"cat <<-EOF\n\trm {silo}/a.md\n\tEOF"
    assert _bash(repo, body) == {}
    assert _decision(_bash(repo, f"{body}\nrm {silo}/b.md")) == "ask"


def test_bash_plain_heredoc_does_not_accept_an_indented_terminator(home, repo):
    silo = mw.memory_dir_of(repo)
    body = f"cat <<EOF\nx\n\tEOF\nrm {silo}/a.md\nEOF"
    assert _bash(repo, body) == {}


def test_bash_commit_message_heredoc_with_double_quotes_in_the_body(home, repo):
    silo = mw.memory_dir_of(repo)
    command = f'git commit -m "$(cat <<\'EOF\'\nsay "hi"\nrm {silo}/a.md\nEOF\n)"'
    assert _bash(repo, command) == {}


def test_bash_command_substitution_inside_quotes_is_read(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _decision(_bash(repo, f'echo "$(rm {silo}/a.md)"')) == "ask"
    assert _decision(_bash(repo, f'echo "$(echo "a")" > {silo}/a.md')) == "ask"


def test_bash_backtick_substitution_is_read(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _decision(_bash(repo, f"echo `rm {silo}/a.md`")) == "ask"


# --- one unresolvable word or directory does not switch the analysis off ---------------------


def test_bash_word_with_a_nul_byte_does_not_hide_the_next_command(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _decision(_bash(repo, f"touch a\x00b; rm {silo}/a")) == "ask"


def test_bash_symlink_loop_does_not_hide_the_next_command(home, repo):
    silo = mw.memory_dir_of(repo)
    (repo / "loop").symlink_to(repo / "loop2")
    (repo / "loop2").symlink_to(repo / "loop")
    assert _decision(_bash(repo, f"touch loop/x; rm {silo}/a")) == "ask"


def test_invalid_declared_directory_leaves_the_default_silo_protected(home, repo):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": "bad\x00dir"}), encoding="utf-8"
    )
    default = home / ".claude" / "projects" / encode(repo) / "memory"
    assert _decision(_bash(repo, f"touch {default}/a.md")) == "ask"
    out = mw.build_output(_payload("Write", repo, file_path=str(default / "a.md")))
    assert _decision(out) == "ask"


def test_invalid_declared_directory_does_not_hide_other_levels(home, repo):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": "bad\x00dir"}), encoding="utf-8"
    )
    parent_memory = home / "parent-mem"
    (repo.parent / ".claude").mkdir()
    (repo.parent / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": str(parent_memory)}), encoding="utf-8"
    )
    assert _decision(_bash(repo, f"touch {parent_memory}/a.md")) == "ask"


# --- value flags, `-` and the command-position check ----------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "touch --reference {d}/a.md /tmp/b",
        "truncate --reference {d}/a.md /tmp/b",
        "truncate -r {d}/a.md /tmp/b",
        "sed -i -f {d}/s.sed /tmp/x",
        "sed -i --file {d}/s.sed /tmp/x",
        "sed -i -e s/a/b/ -f {d}/s.sed /tmp/x",
    ],
)
def test_bash_value_flag_arguments_are_not_targets(home, repo, template):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, template.format(d=silo)) == {}


def test_bash_truncate_size_flag_still_targets_the_file(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _decision(_bash(repo, f"truncate -s 0 {silo}/a.md")) == "ask"
    assert _decision(_bash(repo, f"truncate --size=0 {silo}/a.md")) == "ask"


def test_bash_stdout_dash_is_not_a_path(home, repo):
    silo = mw.memory_dir_of(repo)
    silo.mkdir(parents=True)
    command = "wget -qO- https://example.com/a"
    assert mw.build_output(_payload("Bash", silo, command=command)) == {}


def test_bash_sed_script_is_not_a_path(home, repo):
    silo = mw.memory_dir_of(repo)
    silo.mkdir(parents=True)
    out = mw.build_output(_payload("Bash", silo, command="sed -i s/a/b/ /tmp/x"))
    assert out == {}


@pytest.mark.parametrize(
    "template",
    [
        "echo rm {d}/a.md",
        "echo touch {d}/a.md",
        "cat {d}/touch",
        "cat {d}/rm",
        "ls tee {d}",
        "printf '%s' cp {d}/a.md",
    ],
)
def test_bash_verb_outside_command_position_is_not_a_write(home, repo, template):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, template.format(d=silo)) == {}


# --- levels, symlinks, redirections --------------------------------------------------------


def test_declared_directory_of_a_parent_level_asks(home, repo):
    target = home / "parent-mem"
    (repo.parent / ".claude").mkdir()
    (repo.parent / ".claude" / "settings.local.json").write_text(
        json.dumps({"autoMemoryDirectory": str(target)}), encoding="utf-8"
    )
    out = mw.build_output(_payload("Write", repo, file_path=str(target / "a.md")))
    assert _decision(out) == "ask"


def test_real_directory_declared_and_written_through_a_symlink_asks(home, repo):
    real = home / "real-mem"
    real.mkdir()
    link = home / "link-mem"
    link.symlink_to(real)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": str(real)}), encoding="utf-8"
    )
    out = mw.build_output(_payload("Write", repo, file_path=str(link / "a.md")))
    assert _decision(out) == "ask"
    assert _decision(_bash(repo, f"touch {link}/a.md")) == "ask"


def test_bash_copy_with_a_noop_redirection_still_asks(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _decision(_bash(repo, f"cp /tmp/a {silo}/a 2>/dev/null")) == "ask"


# --- failing open ----------------------------------------------------------------------------


def test_internal_error_fails_open(home, repo, monkeypatch, capsys):
    def boom(_payload: dict) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(mw, "build_output", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash"})))
    assert mw.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "boom" in captured.err


def test_bash_backslash_newline_continues_the_command(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _decision(_bash(repo, f"rm \\\n  {silo}/a.md")) == "ask"
    assert _decision(_bash(repo, f"rm \\\n{silo}/a.md")) == "ask"


# --- the index is exempt


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_index_file_write_has_no_decision(home, repo, tool):
    silo = mw.memory_dir_of(repo)
    out = mw.build_output(_payload(tool, repo, file_path=str(silo / "MEMORY.md")))
    assert out == {}


@pytest.mark.parametrize(
    "template",
    [
        "echo x >> {d}/MEMORY.md",
        "sed -i 's/a/b/' {d}/MEMORY.md",
        "cp /tmp/x {d}/MEMORY.md",
        "mv {d}/note.md {d}/MEMORY.md",
        "tee -a {d}/MEMORY.md",
        "cat <<EOF > {d}/MEMORY.md\nx\nEOF",
    ],
)
def test_bash_write_to_the_index_has_no_decision(home, repo, template):
    silo = mw.memory_dir_of(repo)
    assert _bash(repo, template.format(d=silo)) == {}


def test_note_next_to_the_index_still_asks(home, repo):
    silo = mw.memory_dir_of(repo)
    assert _decision(_bash(repo, f"mv {silo}/MEMORY.md {silo}/note.md")) == "ask"
    assert _decision(_bash(repo, f"cp {silo}/MEMORY.md {silo}/note.md")) == "ask"


@pytest.mark.parametrize(
    "name", ["sub/MEMORY.md", "memory.md", "MEMORY.md.bak", "x/y/MEMORY.md"]
)
def test_other_files_named_like_the_index_still_ask(home, repo, name):
    silo = mw.memory_dir_of(repo)
    out = mw.build_output(_payload("Write", repo, file_path=str(silo / name)))
    assert _decision(out) == "ask"
    assert _decision(_bash(repo, f"echo x > {silo}/{name}")) == "ask"


def test_index_name_outside_any_memory_directory_has_no_decision(home, repo):
    out = mw.build_output(_payload("Write", repo, file_path=str(repo / "MEMORY.md")))
    assert out == {}


def test_index_of_a_declared_directory_is_exempt_and_its_notes_ask(home, repo):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": "notes/mem"}), encoding="utf-8"
    )
    declared = repo / "notes" / "mem"
    index = mw.build_output(
        _payload("Edit", repo, file_path=str(declared / "MEMORY.md"))
    )
    note = mw.build_output(_payload("Write", repo, file_path=str(declared / "a.md")))
    assert index == {}
    assert _decision(note) == "ask"
    nested = declared / "sub" / "MEMORY.md"
    assert (
        _decision(mw.build_output(_payload("Write", repo, file_path=str(nested))))
        == "ask"
    )


def test_index_of_a_parent_level_is_exempt(home, repo):
    parent_memory = home / "parent-mem"
    (repo.parent / ".claude").mkdir()
    (repo.parent / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": str(parent_memory)}), encoding="utf-8"
    )
    index = _payload("Write", repo, file_path=str(parent_memory / "MEMORY.md"))
    assert mw.build_output(index) == {}
    other = _payload("Write", repo, file_path=str(parent_memory / "a.md"))
    assert _decision(mw.build_output(other)) == "ask"


def test_index_of_another_projects_default_silo_is_exempt(home, repo):
    other = home / ".claude" / "projects" / "-some-other-project" / "memory"
    assert (
        mw.build_output(_payload("Write", repo, file_path=str(other / "MEMORY.md")))
        == {}
    )
    nested = other / "sub" / "MEMORY.md"
    assert (
        _decision(mw.build_output(_payload("Write", repo, file_path=str(nested))))
        == "ask"
    )


def test_index_reached_through_a_symlink_is_exempt(home, repo):
    silo = mw.memory_dir_of(repo)
    silo.mkdir(parents=True)
    link = home / "link-mem"
    link.symlink_to(silo)
    out = mw.build_output(_payload("Write", repo, file_path=str(link / "MEMORY.md")))
    assert out == {}
