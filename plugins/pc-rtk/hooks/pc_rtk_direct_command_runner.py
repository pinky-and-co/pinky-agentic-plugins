#!/usr/bin/env python3
"""UserPromptExpansion hook runner — run a deterministic skill script with no model call.

A skill whose whole job is to run one script and echo its output pays a full model turn
for nothing. When the user types that slash command directly, a `UserPromptExpansion`
hook fires before any model call: this runner checks the typed arguments against a
declared grammar, runs the script, and blocks the expansion with the script's output as
the message shown to the user (`reason`). The model never sees that output: Claude Code
does not forward `additionalContext` alongside `decision: block`.

The hook always fails toward the skill, never toward a block: no matching form, a
malformed grammar, a missing script, a non-zero exit or a timeout all print nothing and
exit 0, so the skill then runs normally.

Invoked once per command from a plugin's `hooks.json` (the registry lives there, inline),
with `matcher` set to a regex on the command name:

    direct_command_runner.py --script <abs path> --grammar '<json>'
                             [--interpreter python3|bash] [--timeout 45]

Grammar: `{"forms": [{"match": [<spec>...], "emit": [<str>...]}]}`. A spec is a literal
string, `{"re": "<regex>"}` (one token, full match) or `{"re": "<regex>", "repeat": true}`
(one or more tokens, last spec only). A regex token never starts with `-` unless the spec
carries `"allow_dash": true`, so a value can never masquerade as a flag. `emit` is the argv
appended after the script; `{0}`, `{1}`... are matched tokens, `{*}` all of them, and
`{env:NAME}` an environment variable (`CLAUDE_PROJECT_DIR` falls back to the event cwd).

Arguments are never handed to a shell: any shell metacharacter, newline or NUL in the raw
argument string makes it non-conforming whatever the grammar says.

POSIX only (`os.killpg`). The skill re-runs the script whenever this runner stays silent, so
every registered script must be idempotent. Python 3.12+, stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
from pathlib import Path

DEFAULT_TIMEOUT = 45.0
INTERPRETERS = ("python3", "bash")

# Toute métacaractère shell, retour ligne ou NUL disqualifie l'argument entier, même entre guillemets.
_FORBIDDEN = re.compile(r"[\x00\n\r`$;&|<>()]")
_PLACEHOLDER = re.compile(r"\{(\d+|env:[A-Za-z_][A-Za-z0-9_]*)\}")


def _tokenize(command_args: str) -> list[str] | None:
    """Split the raw argument string, or reject it.

    Args:
        command_args: The arguments typed after the command name.

    Returns:
        The tokens, or None when the string is non-conforming.
    """
    if _FORBIDDEN.search(command_args):
        return None
    try:
        return shlex.split(command_args)
    except ValueError:
        return None


def _token_matches(word: str, spec: object) -> bool:
    """Check one token against one spec.

    Args:
        word: One token from the typed arguments.
        spec: A literal string or a `{"re": ...}` mapping.

    Returns:
        True when the token satisfies the spec.
    """
    if isinstance(spec, str):
        return word == spec
    if isinstance(spec, dict) and isinstance(spec.get("re"), str):
        if word.startswith("-") and not spec.get("allow_dash"):
            return False
        return re.fullmatch(spec["re"], word) is not None
    return False


def _form_matches(tokens: list[str], specs: list[object]) -> bool:
    """Check a whole token list against a form's `match` list.

    Args:
        tokens: The typed tokens.
        specs: The form's specs; only the last one may carry `"repeat": true`.

    Returns:
        True when every token is consumed and every spec is satisfied.
    """
    last = specs[-1] if specs else None
    if isinstance(last, dict) and last.get("repeat"):
        head = specs[:-1]
        if len(tokens) < len(head) + 1:
            return False
        return all(_token_matches(t, s) for t, s in zip(tokens, head)) and all(
            _token_matches(t, last) for t in tokens[len(head) :]
        )
    return len(tokens) == len(specs) and all(
        _token_matches(t, s) for t, s in zip(tokens, specs)
    )


def _resolve_placeholder(name: str, tokens: list[str], cwd: str) -> str:
    """Resolve one `{...}` placeholder of an `emit` string.

    Args:
        name: The placeholder body, a token index or `env:NAME`.
        tokens: The matched tokens.
        cwd: The event working directory.

    Returns:
        The replacement text.

    Raises:
        ValueError: When an environment variable is neither set nor resolvable.
    """
    if name.isdigit():
        return tokens[int(name)]
    var = name.removeprefix("env:")
    value = os.environ.get(var) or (cwd if var == "CLAUDE_PROJECT_DIR" else "")
    if not value:
        raise ValueError(var)
    return value


def _expand(emit: list[str], tokens: list[str], cwd: str) -> list[str]:
    """Build the argv appended after the script.

    Args:
        emit: The form's `emit` strings.
        tokens: The matched tokens.
        cwd: The event working directory.

    Returns:
        The expanded argv entries; a lone `{*}` splices every token as its own entry.
    """
    argv: list[str] = []
    for item in emit:
        if item == "{*}":
            argv.extend(tokens)
        else:
            argv.append(
                _PLACEHOLDER.sub(
                    lambda m: _resolve_placeholder(m.group(1), tokens, cwd), item
                )
            )
    return argv


def _select_argv(grammar: object, tokens: list[str], cwd: str) -> list[str] | None:
    """Pick the first form matching the tokens and expand its `emit`.

    Args:
        grammar: The parsed grammar JSON.
        tokens: The typed tokens.
        cwd: The event working directory.

    Returns:
        The argv for the script, or None when no form matches.
    """
    forms = grammar.get("forms") if isinstance(grammar, dict) else None
    for form in forms or []:
        if _form_matches(tokens, form["match"]):
            return _expand(form["emit"], tokens, cwd)
    return None


def _command(script: str, interpreter: str | None) -> list[str]:
    """Build the executable prefix of the child command.

    Args:
        script: Absolute path of the skill script.
        interpreter: `python3`, `bash` or None to execute the script directly.

    Returns:
        The command list before the emitted arguments.

    Raises:
        ValueError: When the interpreter is not allowed or the script is missing.
    """
    if interpreter is not None and interpreter not in INTERPRETERS:
        raise ValueError(interpreter)
    if not Path(script).is_file():
        raise ValueError(script)
    if interpreter is None:
        return [script]
    return [sys.executable if interpreter == "python3" else "bash", script]


def _run(args: argparse.Namespace, event: object) -> str | None:
    """Run the script for a conforming invocation.

    Args:
        args: The parsed runner options.
        event: The hook event JSON.

    Returns:
        The script's stripped stdout (or `(no output)`), or None when nothing should
        be blocked.
    """
    if not isinstance(event, dict):
        return None
    tokens = _tokenize(str(event.get("command_args") or ""))
    if tokens is None:
        return None
    cwd = str(event.get("cwd") or os.getcwd())
    if not Path(cwd).is_dir():
        return None
    argv = _select_argv(json.loads(args.grammar), tokens, cwd)
    if argv is None:
        return None
    # Session dédiée : sur timeout on tue tout le groupe, sinon un petit-enfant garde le pipe ouvert.
    proc = subprocess.Popen(  # nosec B603 - liste, jamais de shell
        [*_command(args.script, args.interpreter), *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        stdout, _ = proc.communicate(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return None
    if proc.returncode != 0:
        return None
    return stdout.strip() or "(no output)"


def main() -> int:
    """Read the hook event, run the script, and block with its output on success.

    Returns:
        Always 0 — every failure path stays silent so the skill takes over.
    """
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("--script", required=True)
        parser.add_argument("--grammar", required=True)
        parser.add_argument("--interpreter", default=None)
        parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
        text = _run(parser.parse_args(), json.loads(sys.stdin.read() or "{}"))
    except (Exception, SystemExit):  # noqa: BLE001 - un hook ne doit jamais bloquer
        return 0
    if text is not None:
        payload = {"decision": "block", "reason": text}
        try:
            print(json.dumps(payload))
        except (OSError, UnicodeError):
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
