#!/usr/bin/env python3
"""Local runtime — run a task on a LOCAL open-weight model, zero cloud egress.

Escape-hatch pattern: when a task must not reach the cloud model (sensitive file content, a privacy
guard denial), this runs the task against a local model instead — so neither the file content nor
the model's answer ever reaches the cloud model. The answer is written to a local file; **only a
status line (never the answer body) goes to stdout**, so a caller capturing this script's output
cannot leak the result back into the cloud context. That invariant is the whole point — the `offline`
MCP tool (`mcp/pc_core_server.py`) that wraps this module's functions must never return the file
content either, only the status line.

Dual-use: this module is both a standalone CLI (below) and an importable library for
`mcp/pc_core_server.py`, which calls its functions directly rather than shelling out to a subprocess.

Runtime: a local MLX server (oMLX on ``127.0.0.1:11234``) via the `pi` CLI
(``subprocess.run(["pi", "-p", "--model", "mlx/<id>", ...])``) — the same `kind: "cli"` shape
`model_adversarial.py`'s pool already uses for local models, resolved against the `mlx` provider
declared in `~/.pi/agent/models.json` (this script depends on that declaration, never duplicates it).
Default model ``ornith-ai/Ornith-1.5-9B-MLX-6bit`` (bench: 5/7 oracle lines vs 1/7 for gemma-4-12b). ``--thinking off`` is mandatory — a thinking pass drowns a small
local model into an empty answer. Override with ``--model`` / ``PINKY_LOCAL_MODEL``.

MLX-SERVE's own preflight (a `503` body containing `out_of_memory`) fires on the generation request
itself, not on a cheap upfront ping — a separate pre-call would load the model twice, the exact RAM
contention this runtime exists to avoid. `failure_hint()` classifies the real call's failure after
the fact instead: a human-readable hint, never an exception that propagates.

The `pi` call carries ~4k tokens of agent scaffolding whatever the task, whose KV cache needs
~3 GB; oMLX's default memory guard serves it on an 18 GB machine (measured: cold call under 30 s) — a refused call surfaces as BLOCKED through `failure_hint()`.

``--max-tokens`` / ``PINKY_LOCAL_MAX_TOKENS`` only scales this script's own subprocess timeout
budget for a task expected to run long — `pi` itself exposes no generation-length flag to cap.

Usage:
    model_offline.py --task "<task>" [--file PATH ...] [--out PATH] [--model NAME] [--max-tokens N]
"""

from __future__ import annotations

import argparse
import errno
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import call_scope

# Le codepage console par défaut de Windows (cp1252, pas UTF-8) ne peut pas encoder les ✓/⚠ ci-dessous.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

DEFAULT_MODEL = "ornith-ai/Ornith-1.5-9B-MLX-6bit"
PROVIDER = "mlx"
DEFAULT_TOKEN_BUDGET = 3000

# Signal de refus préflight de MLX-SERVE (corps 503).
_OUT_OF_MEMORY_RE = re.compile(r"out_of_memory|GPU memory")


class FailureHint(NamedTuple):
    """A `call_mlx` failure classified into an exit code + a fixed, caller-safe hint string."""

    code: int
    hint: str


def resolve_model(cli_model: str | None = None) -> str:
    """CLI flag > PINKY_LOCAL_MODEL env > default. Keeps the runtime model-agnostic."""
    return cli_model or os.environ.get("PINKY_LOCAL_MODEL") or DEFAULT_MODEL


def resolve_max_tokens(cli_value: int | None = None) -> int:
    """CLI flag > PINKY_LOCAL_MAX_TOKENS env > default. A token *budget* used only to scale this
    script's own subprocess timeout (`main`'s `timeout = max(900, max_tokens // 3)`) — never a
    generation cap, `pi` has none. Same override pattern as `resolve_model`.
    """
    if cli_value is not None:
        return cli_value
    env = os.environ.get("PINKY_LOCAL_MAX_TOKENS")
    return int(env) if env else DEFAULT_TOKEN_BUDGET


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


def is_out_of_memory(text: str) -> bool:
    """Whether `text` (a failed `pi` call's combined stdout+stderr) shows an MLX-SERVE memory
    refusal — either the preflight `503 out_of_memory` (before the model loads) or the runtime
    `400` "requires ~NMB GPU memory but only ~MMB available" (the model is loaded, but this
    prompt's KV cache doesn't fit) — confirmed live: a large context size accepted at the config
    level still hits this second form under real memory pressure.
    """
    return bool(_OUT_OF_MEMORY_RE.search(text))


def _stream(
    proc: subprocess.Popen[str], timeout: int, progress: Callable[[str], None]
) -> tuple[str, str]:
    """Read `proc`'s output as it comes, handing each stdout line to `progress`, until it exits.

    Args:
        proc: the running `pi`, both pipes open in text mode.
        timeout: seconds before `subprocess.TimeoutExpired`.
        progress: called with each stdout line.

    Returns:
        The whole stdout and stderr.
    """
    out: list[str] = []
    err: list[str] = []

    def pump_out() -> None:
        """Collect stdout line by line, reporting each."""
        assert proc.stdout is not None
        for line in proc.stdout:
            out.append(line)
            progress(line)

    def pump_err() -> None:
        """Collect stderr whole."""
        assert proc.stderr is not None
        err.append(proc.stderr.read())

    pumps = [threading.Thread(target=f, daemon=True) for f in (pump_out, pump_err)]
    for pump in pumps:
        pump.start()
    proc.wait(timeout=timeout)
    for pump in pumps:
        pump.join()
    return "".join(out), "".join(err)


def call_mlx(
    model: str,
    prompt: str,
    timeout: int = 900,
    progress: Callable[[str], None] | None = None,
) -> str:
    """Run `pi -p --model 'mlx/<id>' -t read --thinking off "<prompt>"`, return stdout.

    `progress`, when given, is called with each line of the answer as `pi` prints it; it must
    never pass the text on (the caller counts it).

    Raises `subprocess.CalledProcessError` / `subprocess.TimeoutExpired` / `OSError` on failure —
    callers classify those via `failure_hint()`, never letting `pi`'s own stdout/stderr (which may
    echo the sensitive prompt) escape past a fixed status string. Raises `call_scope.CallCancelled`
    when the tool call that asked for this run was cancelled; that one is not a failure to classify.
    """
    cmd = [
        "pi",
        "-p",
        "--model",
        f"{PROVIDER}/{model}",
        "-t",
        "read",
        "--thinking",
        "off",
        prompt,
    ]
    # stdin fermé : pi lit l'entrée standard dont il hérite, et celle du serveur MCP est le canal
    # JSON-RPC — l'appel resterait bloqué jusqu'au timeout.
    call_scope.check()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    call_scope.adopt(proc)
    try:
        if progress is None:
            stdout, stderr = proc.communicate(timeout=timeout)
        else:
            stdout, stderr = _stream(proc, timeout, progress)
    except BaseException:
        # Aussi TimeoutExpired, KeyboardInterrupt : le groupe entier part, pas seulement pi.
        call_scope.kill_session(proc)
        # Avec `progress`, les deux pompes lisent encore les tubes : on attend la fin sans relire.
        if progress is None:
            proc.communicate()
        else:
            proc.wait()
        raise
    finally:
        call_scope.release(proc)
    # Tué par l'annulation de l'appel : pas une réponse.
    call_scope.check()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, stdout, stderr)
    return stdout


def failure_hint(exc: Exception, model: str, timeout: int) -> FailureHint:
    """Map a `call_mlx` failure to a `FailureHint` — the hint is always a fixed string, NEVER `pi`'s
    own output (it may echo the sensitive prompt). `2` = BLOCKED (environment/provider refused to
    serve), `1` = ERROR (the call started but didn't finish). Never raises.
    """
    if isinstance(exc, subprocess.CalledProcessError):
        combined = f"{exc.stdout or ''}\n{exc.stderr or ''}"
        if is_out_of_memory(combined):
            return FailureHint(
                2,
                f"local MLX server out of memory ({model}); "
                "shrink the --file inputs or free RAM on the server.",
            )
        return FailureHint(
            2,
            f"local MLX server call failed (exit {exc.returncode}); check it's running.",
        )
    if isinstance(exc, subprocess.TimeoutExpired):
        return FailureHint(1, f"MLX-SERVE call timed out after {timeout}s.")
    if isinstance(exc, FileNotFoundError):
        return FailureHint(2, "`pi` not found on PATH; install it first.")
    if isinstance(exc, OSError) and exc.errno == errno.E2BIG:
        return FailureHint(
            2,
            "Prompt too large for a single command-line argument (OS argv limit). "
            "Shrink the task or the --file inputs.",
        )
    return FailureHint(2, f"MLX-SERVE call failed to start: {exc.__class__.__name__}.")


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
        f"/tmp/pinky-local-{resolve_session_prefix()}-{datetime.now():%H%M%S}.md"  # nosec B108 — ephemeral scratch file, zero-egress design already scopes what lands here; not a secret, no symlink-attack surface for a local single-user CLI/MCP call
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
    max_tokens = resolve_max_tokens(args.max_tokens)

    prompt = build_prompt(args.task, args.files)
    t = time.time()
    timeout = max(
        900, max_tokens // 3
    )  # dépasse les 900s par défaut pour une grande tâche reproductive via --max-tokens
    try:
        body = call_mlx(model, prompt, timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        fh = failure_hint(e, model, timeout)
        label = "BLOCKED" if fh.code == 2 else "ERROR"
        print(f"[local] {label} — {fh.hint}", file=sys.stderr)
        return fh.code
    dur = time.time() - t

    out = Path(args.out) if args.out else default_out()
    out.write_text(body or "(empty response)", encoding="utf-8")

    # INVARIANT ZÉRO-EGRESS : n'émettre que le chemin + les métriques — JAMAIS le corps de la
    # réponse (il peut dériver d'une entrée sensible, et ce stdout est recapturé côté cloud).
    print(f"[local] {model} · {dur:.0f}s → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
