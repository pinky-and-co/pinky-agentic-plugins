#!/usr/bin/env python3
"""PreToolUse hook: ask the user before anything is written into a memory directory.

A note written to memory reads as a suggestion; a step of a SKILL is a point of passage. When
Claude is told it skipped a step it tends to write a `feedback_*` note instead of fixing the
SKILL. This hook intercepts the write and returns `permissionDecision: ask`, with a reminder of
the procedure as the reason: the user decides each note, nothing is blocked.

Intercepted:
- `Write`, `Edit`, `MultiEdit` whose `file_path` lies inside a memory directory;
- `Bash` commands, split into simple commands at `;`, `&&`, `||`, `|`, `&`, `(` and newlines
  (heredoc bodies, comments and quoted text are never looked into), that write a memory directory:
  a redirection (`>`, `>>`, `>|`, `&>`) whose target is in memory; `tee`, `rm`, `touch`,
  `truncate`, `sed -i`/`--in-place`, `perl -i` with a memory path argument; `cp`, `mv`, `ln`,
  `install`, `rsync` with a memory path as the destination (the last argument, or the `-t` /
  `--target-directory` value); `dd of=`; `curl -o`/`--output` and `wget -O`/`-P` values. A verb
  counts only in command position, so a file named `install.md` or a commit message that quotes
  a memory path asks nothing.

A memory directory is the one resolved for the working directory's level or any ancestor level
(`autoMemoryDirectory` when declared, else the default silo, see `pc_memory_lib.py`), whether or
not it exists yet, or any default silo of any project under the config directory.

Limits: the `Bash` net is shallow. An indirect write (a script, a path built at run time, a path
cut by a `cd`, a glob, `${CLAUDE_CONFIG_DIR:-...}`, an interpreter one-liner, `xargs`, a command
substitution inside quotes) passes through. The command is read by a small word splitter, not by
a full shell parser.

Never blocks and never fails the tool call: any error prints nothing and exits 0.

Environment variables, all optional:
  PC_ALLOW_MEMORY_WRITE=1   let the write through without asking
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from pc_memory_lib import config_dir, level_roots, memory_dir_of  # noqa: E402

FILE_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})
REASON = (
    "Write into memory. A step of a procedure that was skipped: fix the SKILL concerned "
    "(a diff of that SKILL), not a note. A general rule: promote it to a CLAUDE.md or a rule at "
    "the right level. A local trap: a note is acceptable. Allow only if a note is the right place."
)
_HEREDOC = re.compile(r"<<(?!<)-?\s*(['\"]?)([A-Za-z_]\w*)\1")
_ENV_ASSIGNMENT = re.compile(r"^\w+=")
_SED_IN_PLACE = re.compile(r"^(?:-[nEsrzu]*i|--in-place)")
_PERL_IN_PLACE = re.compile(r"^-[pnlaw0-9]*i")
_PREFIX_WORDS = frozenset({"command", "nohup", "time", "then", "do", "else", "!", "{"})
_WRAPPERS = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"}),
    "env": frozenset({"-u", "-C", "-S"}),
}
_ANY_OPERAND = frozenset({"rm", "touch", "truncate", "tee"})
_DESTINATION = frozenset({"cp", "mv", "ln", "install", "rsync"})
_TARGET_DIR_VERBS = frozenset({"cp", "mv", "ln", "install"})
_VALUE_FLAGS = {
    "touch": frozenset({"-r", "-d", "-t"}),
    "truncate": frozenset({"-r", "-s"}),
}
_FLAG_VALUES = {
    "curl": (frozenset("o"), frozenset({"--output"})),
    "wget": (
        frozenset("OP"),
        frozenset({"--output-document", "--directory-prefix"}),
    ),
}
_TARGET_DIR_FLAGS = (frozenset("t"), frozenset({"--target-directory"}))
_DUP_FD = re.compile(r"&(?:\d+|-)")


@dataclass
class Command:
    """One simple command: its words, and the targets of its `>` redirections."""

    words: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)


def _strip_heredocs(command: str) -> str:
    """`command` without the bodies (and terminator lines) of its heredocs."""
    kept: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in command.split("\n"):
        if pending:
            delimiter, dash = pending[0]
            if (line.lstrip("\t") if dash else line) == delimiter:
                pending.pop(0)
            continue
        kept.append(line)
        pending = [
            (m.group(2), m.group(0).startswith("<<-")) for m in _HEREDOC.finditer(line)
        ]
    return "\n".join(kept)


class _Splitter:
    """Single pass over a command line: simple commands, quotes removed, redirections apart."""

    def __init__(self) -> None:
        self.commands: list[Command] = [Command()]
        self.current: list[str] = []
        self.in_word = False
        self.pending: str | None = None

    def flush(self) -> None:
        """End the word being read, filing it as a word, a redirection target or a source."""
        if self.in_word:
            text = "".join(self.current)
            if self.pending == "target":
                self.commands[-1].targets.append(text)
            elif self.pending is None:
                self.commands[-1].words.append(text)
            self.pending = None
        self.current = []
        self.in_word = False

    def boundary(self) -> None:
        """End the simple command being read."""
        self.flush()
        self.pending = None
        if self.commands[-1].words or self.commands[-1].targets:
            self.commands.append(Command())

    def add(self, char: str) -> None:
        """Append a literal character to the current word."""
        self.current.append(char)
        self.in_word = True

    def redirect(self, text: str, pos: int) -> int:
        """Read a `>` or `<` operator at `pos`; return the position after it."""
        if text[pos] == "<":
            self.flush()
            end = pos + 1
            while end < len(text) and text[end] == "<":
                end += 1
            self.pending = "source"
            return end
        if self.in_word and "".join(self.current).isdigit():
            self.current = []
            self.in_word = False
        else:
            self.flush()
        end = pos + 1
        if text.startswith(">", end) or text.startswith("|", end):
            end += 1
        dup = _DUP_FD.match(text, end)
        if dup:
            return dup.end()
        if text.startswith("&", end):
            end += 1
        self.pending = "target"
        return end


def _split_commands(command: str) -> list[Command]:
    """Simple commands of a shell line, honouring quotes, backslashes and comments."""
    text = _strip_heredocs(command)
    sp = _Splitter()
    pos, size = 0, len(text)
    while pos < size:
        char = text[pos]
        nxt = text[pos + 1] if pos + 1 < size else ""
        if char == "\\":
            if nxt and nxt != "\n":
                sp.add(nxt)
            pos += 2
        elif char == "'":
            end = text.find("'", pos + 1)
            end = size if end < 0 else end
            sp.in_word = True
            sp.current.extend(text[pos + 1 : end])
            pos = end + 1
        elif char == '"':
            sp.in_word = True
            pos += 1
            while pos < size and text[pos] != '"':
                if text[pos] == "\\" and pos + 1 < size and text[pos + 1] in '"\\$`':
                    pos += 1
                sp.current.append(text[pos])
                pos += 1
            pos += 1
        elif char in " \t":
            sp.flush()
            pos += 1
        elif char == "#" and not sp.in_word:
            end = text.find("\n", pos)
            pos = size if end < 0 else end
        elif char in ";\n`()":
            sp.boundary()
            pos += 1
        elif char == "|" or (char == "&" and nxt != ">"):
            sp.boundary()
            pos += 2 if nxt == char else 1
        elif char in "<>" or char == "&":
            pos = sp.redirect(text, pos + 1 if char == "&" else pos)
        else:
            sp.add(char)
            pos += 1
    sp.boundary()
    return [c for c in sp.commands if c.words or c.targets]


def memory_dirs(cwd: Path) -> list[Path]:
    """Memory directories of the working directory's level and of each ancestor level."""
    dirs: list[Path] = []
    for root in level_roots(cwd):
        candidate = memory_dir_of(root)
        for variant in (candidate, candidate.resolve()):
            if variant not in dirs:
                dirs.append(variant)
    return dirs


def _inside(path: Path, directory: Path) -> bool:
    """True when `path` is `directory` or lies under it."""
    return path == directory or directory in path.parents


def _is_silo(path: Path) -> bool:
    """True when `path` is inside `<config dir>/projects/<project>/memory`."""
    for base in {config_dir(), config_dir().resolve()}:
        try:
            parts = path.relative_to(base).parts
        except ValueError:
            continue
        if len(parts) >= 3 and parts[0] == "projects" and parts[2] == "memory":
            return True
    return False


def _normalized(word: str, cwd: Path) -> Path:
    """Absolute, normalized path a shell word names.

    `~`, `$HOME` and `${HOME}` are expanded and a relative word resolves from `cwd`.
    """
    home = str(Path.home())
    for spelling in ("${HOME}", "$HOME"):
        if word == spelling or word.startswith(spelling + "/"):
            word = home + word[len(spelling) :]
    path = Path(os.path.expanduser(word))
    return Path(os.path.normpath(path if path.is_absolute() else cwd / path))


def _hits_memory(word: str, cwd: Path, dirs: list[Path]) -> bool:
    """True when the shell word names a path inside a memory directory."""
    if not word:
        return False
    path = _normalized(word, cwd)
    return any(
        _is_silo(variant) or any(_inside(variant, d) for d in dirs)
        for variant in (path, path.resolve())
    )


def file_write_hits_memory(file_path: str, cwd: Path, dirs: list[Path]) -> bool:
    """True when `file_path` (relative paths resolve from `cwd`) is inside a memory directory."""
    return _hits_memory(file_path, cwd, dirs)


def _operands(args: list[str], value_flags: frozenset[str] = frozenset()) -> list[str]:
    """Arguments that are neither flags nor the value of a flag in `value_flags`."""
    out: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in value_flags:
            skip = True
        elif not arg.startswith("-"):
            out.append(arg)
    return out


def _flag_values(
    args: list[str], short: frozenset[str], long: frozenset[str]
) -> list[str]:
    """Values given to the flags `short` (single letters, grouped or glued) and `long`."""
    values: list[str] = []
    for index, arg in enumerate(args):
        following = args[index + 1 : index + 2]
        if arg.startswith("--"):
            name, sep, value = arg.partition("=")
            if name in long:
                values.extend([value] if sep else following)
        elif arg.startswith("-") and len(arg) > 1:
            letters = arg[1:]
            for place, letter in enumerate(letters):
                if letter in short:
                    rest = letters[place + 1 :]
                    values.extend([rest] if rest else following)
                    break
    return values


def _without_wrappers(words: list[str]) -> list[str]:
    """`words` without the leading env assignments, `sudo`, `env` and similar wrappers."""
    words = list(words)
    while words:
        head = words[0]
        if head in _PREFIX_WORDS or _ENV_ASSIGNMENT.match(head):
            words.pop(0)
        elif head in _WRAPPERS:
            words.pop(0)
            while words and words[0].startswith("-"):
                flag = words.pop(0)
                if flag in _WRAPPERS[head] and words:
                    words.pop(0)
        else:
            break
    return words


def _command_writes_memory(cmd: Command, cwd: Path, dirs: list[Path]) -> bool:
    """True when one simple command writes into a memory directory."""
    if any(_hits_memory(target, cwd, dirs) for target in cmd.targets):
        return True
    words = _without_wrappers(cmd.words)
    if not words:
        return False
    verb, args = Path(words[0]).name, words[1:]

    def hit(candidates: list[str]) -> bool:
        return any(_hits_memory(c, cwd, dirs) for c in candidates)

    if verb in _ANY_OPERAND:
        return hit(_operands(args, _VALUE_FLAGS.get(verb, frozenset())))
    if verb in _DESTINATION:
        directories = (
            _flag_values(args, *_TARGET_DIR_FLAGS) if verb in _TARGET_DIR_VERBS else []
        )
        if directories:
            return hit(directories)
        operands = _operands(args)
        return bool(operands) and hit(operands[-1:])
    if verb == "sed" and any(_SED_IN_PLACE.match(a) for a in args):
        return hit(_operands(args))
    if verb == "perl" and any(_PERL_IN_PLACE.match(a) for a in args):
        return hit(_operands(args))
    if verb == "dd":
        return hit([a[3:] for a in args if a.startswith("of=")])
    if verb in _FLAG_VALUES:
        return hit(_flag_values(args, *_FLAG_VALUES[verb]))
    return False


def bash_write_hits_memory(command: str, cwd: Path, dirs: list[Path]) -> bool:
    """True when a simple command of `command` writes into a memory directory."""
    return any(_command_writes_memory(c, cwd, dirs) for c in _split_commands(command))


def writes_memory(payload: dict[str, object]) -> bool:
    """True when the tool call of one PreToolUse payload writes into a memory directory."""
    tool = payload.get("tool_name")
    raw_input = payload.get("tool_input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    raw_cwd = payload.get("cwd")
    cwd = Path(
        raw_cwd if isinstance(raw_cwd, str) and raw_cwd else os.getcwd()
    ).resolve()
    dirs = memory_dirs(cwd)
    if tool in FILE_TOOLS:
        file_path = tool_input.get("file_path")
        return isinstance(file_path, str) and file_write_hits_memory(
            file_path, cwd, dirs
        )
    if tool == "Bash":
        command = tool_input.get("command")
        return isinstance(command, str) and bash_write_hits_memory(command, cwd, dirs)
    return False


def build_output(payload: dict[str, object]) -> dict[str, object]:
    """Hook output for one payload: an `ask` decision for a memory write, else an empty dict."""
    if not writes_memory(payload):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": REASON,
        }
    }


def main() -> int:
    """Entry point: read the payload on stdin, print the decision, always exit 0."""
    if os.environ.get("PC_ALLOW_MEMORY_WRITE") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        output = build_output(payload)
    except Exception as exc:  # never fail the tool call
        sys.stderr.write(f"memory_write_guard: {exc}\n")
        return 0
    if output:
        print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
