#!/usr/bin/env python3
"""Manual smoke test — one real call per `model_pool` entry, one entry at a time.

Never collected by pytest (no `test_` prefix / `_test` suffix), never run by any gate — it makes
real network/subprocess calls against live providers and must stay opt-in. Relaunch by hand:

    uv run python3 tests/smoke_model_pool.py

Read-only: never edits `settings.pinky.json`, never writes anywhere but stdout. For each pool
entry it fires the smallest possible prompt through `model_adversarial.run_pool([entry], ...)` —
passing the whole pool at once would stop at the first success and never reach the rest, so each
entry is dispatched alone, in its own `run_pool([entry], ...)` call.

`http`/`cli` entries: a real request/subprocess call. An `offline` entry (if the pool carries one)
is reported as skipped, since `run_pool` refuses to dispatch it by design — it belongs to the
separate zero-egress `offline` MCP tool.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "pc-core" / "mcp"))

import model_adversarial as ma

PROMPT = "Reply with exactly one word: OK"
TIMEOUT = 60


def smoke_one(entry: dict) -> tuple[bool, str]:
    """Fire one minimal call at `entry`. Returns (ok, detail) — never raises."""
    if entry.get("kind") == "offline":
        return (
            False,
            "skipped — offline entries route through the `offline` MCP tool, not this pool path",
        )
    try:
        result = ma.run_pool([entry], PROMPT, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001 — un smoke test ne doit jamais planter sur une entrée
        return False, f"raised {type(e).__name__}: {e}"
    if result.entry is None:
        return False, str(result.result)
    content = ma.extract_content(result.result)
    return True, content.strip()


def main() -> int:
    pool = ma.load_pool()
    print(f"[smoke] {len(pool)} pool entries loaded\n")
    ok_count = 0
    for entry in pool:
        name = entry["name"]
        t = time.time()
        ok, detail = smoke_one(entry)
        dur = time.time() - t
        status = "OK" if ok else "FAIL"
        ok_count += ok
        print(f"[{status}] {name} ({entry.get('kind')}) · {dur:.1f}s")
        print(f"    {detail}\n")
    print(f"[smoke] {ok_count}/{len(pool)} entries answered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
