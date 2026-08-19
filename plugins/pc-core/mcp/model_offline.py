#!/usr/bin/env python3
"""Local runtime — run a task on a LOCAL open-weight model, zero cloud egress.

Escape-hatch pattern: when a task must not reach the cloud model (sensitive file content, a privacy
guard denial), this runs the task against a local Ollama model instead — so neither the file content
nor the model's answer ever reaches the cloud model. The answer is written to a local file; **only a
status line (never the answer body) goes to stdout**, so a caller capturing this script's output
cannot leak the result back into the cloud context. That invariant is the whole point — the `offline`
MCP tool (`mcp/server.py`) that wraps this module's functions must never return the file content
either, only the status line.

Dual-use: this module is both a standalone CLI (below) and an importable library for
`mcp/server.py`, which calls its functions directly rather than shelling out to a subprocess.

Runtime: Ollama HTTP API, stdlib only. Default model ``gemma4:12b``; ``think:false`` is mandatory or
gemma drowns and returns an empty response. Override with ``--model`` / ``PINKY_LOCAL_MODEL`` and
``PINKY_OLLAMA_URL``.

The default 3000-token generation cap fits summarization/analysis tasks but silently truncates
(``done=length``) a task that must reproduce something close to the full input (e.g. redacting PII
in place across a whole document) — override with ``--max-tokens`` / ``PINKY_LOCAL_MAX_TOKENS`` when
the task is reproductive rather than a distillation.

Usage:
    pinky_local.py --task "<task>" [--file PATH ...] [--out PATH] [--model NAME] [--max-tokens N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# Windows' default console codepage (cp1252, not UTF-8) can't encode this script's ✓/⚠ output.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

DEFAULT_MODEL = "gemma4:12b"
DEFAULT_URL = "http://localhost:11434"
NUM_PREDICT = 3000
TEMPERATURE = 0.2


def resolve_model(cli_model: str | None = None) -> str:
    """CLI flag > PINKY_LOCAL_MODEL env > default. Keeps the runtime model-agnostic."""
    return cli_model or os.environ.get("PINKY_LOCAL_MODEL") or DEFAULT_MODEL


def resolve_url() -> str:
    """Ollama base URL from PINKY_OLLAMA_URL, default localhost — always a LOCAL endpoint (zero egress)."""
    return os.environ.get("PINKY_OLLAMA_URL", DEFAULT_URL).rstrip("/")


def resolve_max_tokens(cli_value: int | None = None) -> int:
    """CLI flag > PINKY_LOCAL_MAX_TOKENS env > default. Same override pattern as resolve_model."""
    if cli_value is not None:
        return cli_value
    env = os.environ.get("PINKY_LOCAL_MAX_TOKENS")
    return int(env) if env else NUM_PREDICT


def build_prompt(task: str, files: list[str]) -> str:
    """Assemble the local prompt from the task + each file's content, read locally (never via the cloud)."""
    parts = [
        "You are a local assistant. Perform the TASK on the material below; answer fully and directly.",
        f"--- TASK ---\n{task}",
    ]
    for p in files:
        content = Path(p).read_text(encoding="utf-8", errors="ignore")
        parts.append(f"--- FILE: {p} ---\n{content}")
    return "\n\n".join(parts)


def num_ctx_for(prompt: str, num_predict: int = NUM_PREDICT) -> int:
    """Pick a context window that fits the prompt + generation. Floor 8192, cap 32768."""
    est = len(prompt) // 3 + num_predict  # ~3 chars/token, coarse but safe upward
    for n in (8192, 16384, 32768):
        if est <= n:
            return n
    return 32768


def preflight(url: str, model: str) -> str | None:
    """Return a human hint if Ollama isn't running or the model isn't pulled, else None. Never raises."""
    try:
        tags = json.load(urllib.request.urlopen(f"{url}/api/tags", timeout=5))
    except (urllib.error.URLError, OSError):
        return (
            f"Ollama not reachable at {url}. Start it (`ollama serve` / `brew services start ollama`) "
            f"and pull the model (`ollama pull {model}`). This runtime needs a local Ollama — nothing "
            f"is sent to the cloud."
        )
    names = {m.get("name", "").split(":")[0] for m in tags.get("models", [])}
    if model.split(":")[0] not in names:
        return f"Model `{model}` not pulled. Run `ollama pull {model}` first."
    return None


def call_ollama(
    url: str,
    model: str,
    prompt: str,
    num_ctx: int,
    num_predict: int = NUM_PREDICT,
    timeout: int = 900,
) -> dict:
    """POST to Ollama /api/generate (non-streaming, think:false mandatory for gemma). Returns the JSON."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,  # mandatory for gemma4 (thinking model → empty response otherwise); harmless elsewhere
        "options": {
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "temperature": TEMPERATURE,
        },
    }
    req = urllib.request.Request(
        f"{url}/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def resolve_session_prefix() -> str:
    """First 8 chars of CLAUDE_CODE_SESSION_ID — same convention as agent teams (`session-<8 chars>`).

    Falls back to "standalone" outside a Claude Code session (manual CLI testing).
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    return f"session-{session_id[:8]}" if session_id else "standalone"


def default_out() -> Path:
    """A local, never-committed output path. Session-prefixed for traceability; time-suffixed because,
    unlike the adversarial exchange file, this output survives past the call for the user to open —
    a second call in the same session must not clobber the first before it's read."""
    return Path(
        f"/tmp/pinky-local-{resolve_session_prefix()}-{datetime.now():%H%M%S}.md"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run a task on a local model, zero cloud egress."
    )
    ap.add_argument("--task", required=True)
    ap.add_argument("--file", action="append", default=[], dest="files")
    ap.add_argument("--out")
    ap.add_argument("--model")
    ap.add_argument("--max-tokens", type=int, default=None)
    args = ap.parse_args(argv)

    model = resolve_model(args.model)
    url = resolve_url()
    max_tokens = resolve_max_tokens(args.max_tokens)

    hint = preflight(url, model)
    if hint:
        print(f"[local] BLOCKED — {hint}", file=sys.stderr)
        return 2

    prompt = build_prompt(args.task, args.files)
    t = time.time()
    timeout = max(
        900, max_tokens // 3
    )  # scale past the 900s default for large --max-tokens reproductive tasks
    try:
        r = call_ollama(
            url, model, prompt, num_ctx_for(prompt, max_tokens), max_tokens, timeout
        )
    except (urllib.error.URLError, OSError) as e:
        print(f"[local] ERROR — Ollama call failed: {e}", file=sys.stderr)
        return 1
    dur = time.time() - t

    out = Path(args.out) if args.out else default_out()
    body = (
        r.get("response")
        or "(empty response — gemma drowns if `think` is not disabled)"
    )
    out.write_text(body, encoding="utf-8")

    # ZERO-EGRESS INVARIANT: emit the path + metrics only — NEVER the answer body (it may derive from
    # sensitive input, and this stdout is captured back into the cloud context).
    print(
        f"[local] {model} · {dur:.0f}s · in={r.get('prompt_eval_count')} "
        f"out={r.get('eval_count')} · done={r.get('done_reason')} → {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
