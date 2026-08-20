#!/usr/bin/env python3
"""Adversarial runtime — run a task on a contrarian cloud model, via the Mistral API.

Called by the `adversarial` MCP tool (`mcp/server.py`): it challenges **this session's own output**
(Claude), so a same-family reviewer is exactly what to avoid — the model here must not share Claude's
blind spots. Same call shape as any OpenAI-compatible ``/v1/chat/completions`` runtime, pointed at
``api.mistral.ai``.

Dual-use: this module is both a standalone CLI (below) and an importable library for
`mcp/server.py`, which calls its functions directly rather than shelling out to a subprocess.

Default model ``mistral-medium-3.5`` — pragmatic default: GLM 5.2 (``zai-glm-5-2``, Z.ai's model
served third-party/unmodified on Mistral's API since 2026-08) benchmarks ahead of it for this use,
but returns ``403 tier_not_allowed`` on the account's current Mistral subscription tier (confirmed
2026-08-19). Switch the default to ``zai-glm-5-2`` once the tier is upgraded — until then, override
per-call with ``--model zai-glm-5-2`` / ``PINKY_ADVERSARIAL_MODEL=zai-glm-5-2`` to test tier access.

Auth: ``MISTRAL_API_KEY`` env var, else ``~/.mistral_api_key`` file (the file matters for an
unattended/scheduled caller with no shell env).

Usage:
    model_adversarial.py --task "<task>" [--file PATH ...] [--model NAME]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows' default console codepage (cp1252, not UTF-8) can't encode this script's ✓/⚠ output.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

DEFAULT_MODEL = "mistral-medium-3.5"  # zai-glm-5-2 benchmarks better but needs a Mistral tier upgrade first
API_URL = "https://api.mistral.ai/v1/chat/completions"
MAX_TOKENS = 8000
TEMPERATURE = 0.2


def resolve_model(cli_model: str | None = None) -> str:
    """CLI flag > PINKY_ADVERSARIAL_MODEL env > default (GLM 5.2, benchmarked pick)."""
    return cli_model or os.environ.get("PINKY_ADVERSARIAL_MODEL") or DEFAULT_MODEL


def resolve_api_key() -> str | None:
    """MISTRAL_API_KEY env > ~/.mistral_api_key file. None if neither is set.

    Le fichier n'est pas un fallback de confort : c'est la seule source qu'un job sans shell profile
    (env minimal) puisse lire.
    """
    env_key = os.environ.get("MISTRAL_API_KEY")
    if env_key:
        return env_key.strip()
    key_path = Path.home() / ".mistral_api_key"
    if key_path.exists():
        return key_path.read_text(encoding="utf-8").strip()
    return None


_METHODOLOGY_PATH = (
    Path(__file__).parent.parent / "skills" / "review-guidelines" / "SKILL.md"
)
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n?", re.DOTALL)


def load_methodology() -> str:
    """The shared review methodology — the `review-guidelines` skill's body, frontmatter stripped.

    Single source of truth for what "reviewed" means in this suite: the `reviewer` agent and this
    tool both read the SAME skill (neither depends on the other — a skill file is neutral ground,
    same reasoning as `policy_lib.py` sitting between two hook concerns), so a finding looks the
    same regardless of which model produced it. Empty string if the file is missing rather than
    raising — a caller with no methodology loaded still gets a review, just an unstructured one.
    """
    try:
        text = _METHODOLOGY_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    return _FRONTMATTER_RE.sub("", text, count=1).strip()


def build_prompt(task: str, files: list[str]) -> str:
    """Assemble the prompt from the task + each file's content."""
    parts = [f"--- TASK ---\n{task}"]
    for p in files:
        content = Path(p).read_text(encoding="utf-8", errors="ignore")
        parts.append(f"--- FILE: {p} ---\n{content}")
    return "\n\n".join(parts)


def call_mistral(
    api_key: str,  # gitleaks:allow
    model: str,
    prompt: str,
    timeout: int = 180,
) -> dict:
    """POST to Mistral /v1/chat/completions (OpenAI-compatible, non-streaming)."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    return json.load(urllib.request.urlopen(req, timeout=timeout))  # nosec B310 — req.full_url is API_URL, a hardcoded https:// constant, never attacker-controlled


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run a task on an adversarial cloud model (Mistral API)."
    )
    ap.add_argument("--task", required=True)
    ap.add_argument("--file", action="append", default=[], dest="files")
    ap.add_argument("--model")
    args = ap.parse_args(argv)

    model = resolve_model(args.model)
    api_key = resolve_api_key()  # pragma: allowlist secret
    if not api_key:
        print(
            "[adversarial] BLOCKED — no API key found (MISTRAL_API_KEY env var or ~/.mistral_api_key "
            "file). Create the file with your key from console.mistral.ai.",
            file=sys.stderr,
        )
        return 2

    prompt = build_prompt(args.task, args.files)
    t = time.time()
    try:
        r = call_mistral(api_key, model, prompt)
    except urllib.error.HTTPError as e:
        print(
            f"[adversarial] ERROR — HTTP {e.code}: {e.read().decode(errors='ignore')}",
            file=sys.stderr,
        )
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"[adversarial] ERROR — call failed: {e}", file=sys.stderr)
        return 1
    dur = time.time() - t

    usage = r.get("usage", {})
    choice = r.get("choices", [{}])[0]
    message = choice.get("message", {})
    content = (
        message.get("content") or message.get("reasoning_content") or "(empty response)"
    )
    # Reasoning models return content as a list of typed blocks
    # [{"type":"thinking",...}, {"type":"text","text":...}] — keep only the final answer text.
    if isinstance(content, list):
        texts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        content = "\n".join(t for t in texts if t) or "(no text block — reasoning only)"

    print(
        f"[adversarial] {model} · {dur:.0f}s · in={usage.get('prompt_tokens')} "
        f"out={usage.get('completion_tokens')} · finish={choice.get('finish_reason')}",
        file=sys.stderr,
    )
    print(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
