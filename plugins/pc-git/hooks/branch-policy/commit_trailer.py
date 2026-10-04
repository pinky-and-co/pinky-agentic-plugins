#!/usr/bin/env python3
"""commit_trailer — the attribution trailer of a commit, from the model of the MAIN session.

A forked skill (`context: fork`, `model: haiku`) runs on its own model, so the model it would
name in a trailer is the fork's, not the session's. The session's real model is recorded by the
harness in the session transcript (`<config>/projects/<project>/<session-id>.jsonl`) as an
`attachment` entry of type `model`, whatever the provider (Anthropic, z.ai, Ollama Cloud...).
This script reads it there and prints the trailer, ready for `git commit -m ... -m "<trailer>"`.

Usage:
    python3 commit_trailer.py [--session-id <id>] [--config-dir <path>]

Prints one or two lines and exits 0 whatever is missing — every missing input degrades to the
neutral line `Co-Authored-By: Claude <noreply@anthropic.com>`:
  - no `--session-id`, an id that is not a plain identifier (an unsubstituted `${...}`), no
    transcript under the config directory, or no model entry in it;
  - `Claude-Session: https://claude.ai/code/<id>` is added only when
    `$CLAUDE_CODE_BRIDGE_SESSION_ID` is set and non-empty, never with an empty id.
The config directory is `--config-dir`, else `$CLAUDE_CONFIG_DIR`, else `~/.claude`.

Model name: a model id containing `claude` is an Anthropic model, named from the harness's
marketing name (`Sonnet 5`) or, failing that, from the id (`claude-sonnet-5[1m]` -> `Sonnet 5`).
Any other id is named as the harness configured it, behind the harness itself:
`Claude Code (glm-5.3)`. The `[1m]` context-window suffix is never part of a name.
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

NEUTRAL_TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"
_EMAIL = "noreply@anthropic.com"
# Un id de session est un UUID ; tout ce qui n'est pas un identifiant simple (un `${...}` non
# substitué, un `..`, un `*`) est refusé AVANT d'entrer dans un chemin ou un glob.
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SUFFIX = re.compile(r"\[[^\]]*\]$")
# Les entrées sont écrites en JSON compact ; la tolérance aux espaces évite de dépendre du format.
_MODEL_ATTACHMENT = re.compile(r'"type"\s*:\s*"model"')
_ASSISTANT_ENTRY = re.compile(r'"type"\s*:\s*"assistant"')
_MODEL_FIELD = re.compile(r'"model"\s*:\s*"([^"]+)"')
_OWNER_FIELD = re.compile(r'"sessionId"\s*:\s*"([^"]+)"')
_SIDECHAIN = re.compile(r'"isSidechain"\s*:\s*true')


@dataclass(frozen=True)
class ModelIdentity:
    """The model a session runs on, as the harness recorded it."""

    model_id: str
    marketing_name: str | None = None


def strip_suffix(model_id: str) -> str:
    """Return the model id without its bracketed context-window suffix (`glm-5.3[1m]`)."""
    return _SUFFIX.sub("", model_id.strip()).strip()


def short_model(model_id: str) -> str:
    """Return a short display name from a Claude model id (`claude-opus-4-8` -> `Opus 4.8`)."""
    name = strip_suffix(model_id)
    if not name:
        return "?"
    # Un id préfixé par un fournisseur (`us.anthropic.claude-sonnet-5-v1:0`) : on repart de `claude-`.
    cut = name.lower().find("claude-")
    if cut >= 0:
        name = name[cut + len("claude-") :]
    parts = name.split("-")
    label = parts[0].capitalize()
    # Seuls les groupes de chiffres courts sont une version : une date (`20251001`) ou un
    # suffixe (`v1:0`, `latest`) n'en est pas une.
    version_parts = [p for p in parts[1:] if p.isdigit() and len(p) < 6]
    return f"{label} {'.'.join(version_parts)}".strip()


def is_anthropic_model(model_id: str) -> bool:
    """Whether a model id names an Anthropic model: it contains `claude`, in any case."""
    return "claude" in model_id.lower()


def find_session_file(session_id: str, config_dir: Path) -> Path | None:
    """Locate `<config_dir>/projects/*/<session_id>.jsonl`, or None.

    The project directory is an encoding of the working directory: globbing on the session id
    (a UUID, unique across projects) avoids recomputing that encoding.
    """
    if not _SESSION_ID.match(session_id):
        return None
    for candidate in sorted((config_dir / "projects").glob(f"*/{session_id}.jsonl")):
        if candidate.is_file():
            return candidate
    return None


def _entry_of(line: str, session_id: str) -> object | None:
    """Parse one transcript line; None when truncated, foreign to the session or a sidechain."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    if entry.get("isSidechain") is True:
        return None
    owner = entry.get("sessionId")
    if owner and owner != session_id:
        return None
    return entry


def _identity_of_attachment(entry: object) -> ModelIdentity | None:
    """The identity carried by an `attachment` entry of type `model`, if it is one."""
    if not isinstance(entry, dict):
        return None
    attachment = entry.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "model":
        return None
    identity = attachment.get("identity")
    if not isinstance(identity, dict):
        return None
    model_id = identity.get("modelId")
    if not isinstance(model_id, str) or not strip_suffix(model_id):
        return None
    marketing = identity.get("marketingName")
    return ModelIdentity(model_id, marketing if isinstance(marketing, str) else None)


def _identity_of_assistant(line: str, session_id: str) -> ModelIdentity | None:
    """The model of an assistant line, `<synthetic>` placeholders and foreign lines excluded.

    Read with a regex rather than a parse: assistant lines are the bulk of a transcript and
    carry whole messages, while the model id is a short field near their start.
    """
    if _SIDECHAIN.search(line):
        return None
    owner = _OWNER_FIELD.search(line)
    if owner and owner.group(1) != session_id:
        return None
    found = _MODEL_FIELD.search(line)
    if not found or found.group(1).startswith("<") or not strip_suffix(found.group(1)):
        return None
    return ModelIdentity(found.group(1))


def read_model_identity(path: Path, session_id: str) -> ModelIdentity | None:
    """Return the session's model, read from the transcript at `path`, or None.

    One forward pass, parsing only the lines that can matter: the LAST `model` attachment wins
    (a model switch writes a new one); without any, the last assistant message's `model`.
    A truncated last line (the file is being appended to) is skipped, never an error.
    """
    last_attachment: ModelIdentity | None = None
    last_assistant: ModelIdentity | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if _MODEL_ATTACHMENT.search(line):
                    found = _identity_of_attachment(_entry_of(line, session_id))
                    if found is not None:
                        last_attachment = found
                elif _ASSISTANT_ENTRY.search(line):
                    found = _identity_of_assistant(line, session_id)
                    if found is not None:
                        last_assistant = found
    except OSError:
        return None
    return last_attachment or last_assistant


def build_trailer(identity: ModelIdentity | None, bridge_session_id: str | None) -> str:
    """Compose the trailer block: the attribution line, plus `Claude-Session` when possible."""
    if identity is None:
        line = NEUTRAL_TRAILER
    elif is_anthropic_model(identity.model_id):
        name = (identity.marketing_name or "").strip() or short_model(identity.model_id)
        if name.lower().startswith("claude"):
            name = name[len("claude") :].strip()
        line = f"Co-Authored-By: Claude {name} <{_EMAIL}>"
    else:
        line = f"Co-Authored-By: Claude Code ({strip_suffix(identity.model_id)}) <{_EMAIL}>"
    if bridge_session_id:
        line = f"{line}\nClaude-Session: https://claude.ai/code/{bridge_session_id}"
    return line


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; always exits 0 so a missing input never breaks a commit."""
    parser = argparse.ArgumentParser(
        description="Print the commit attribution trailer."
    )
    parser.add_argument("--session-id", default="")
    parser.add_argument("--config-dir", default="")
    args = parser.parse_args(argv)

    config_dir = Path(
        args.config_dir
        or os.environ.get("CLAUDE_CONFIG_DIR")
        or Path.home() / ".claude"
    )
    identity: ModelIdentity | None = None
    session_file = find_session_file(args.session_id, config_dir)
    if session_file is not None:
        identity = read_model_identity(session_file, args.session_id)
    print(build_trailer(identity, os.environ.get("CLAUDE_CODE_BRIDGE_SESSION_ID")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
