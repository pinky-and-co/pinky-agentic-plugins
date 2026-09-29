#!/usr/bin/env python3
"""Hook — refresh the active non-Anthropic provider's quota cache, read by the status line's
rate-limit warning (see `scripts/_quota.py`).

Usage: `quota_refresh.py <session|prompt|stop|tool>` — the argument only picks how young a cache is
still good enough. A no-op on Anthropic (its quota comes with the harness payload) and when the
token is missing. Always exits 0 and prints NOTHING on stdout: for `SessionStart` and
`UserPromptSubmit` the harness injects a hook's stdout into the model's context.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import _quota  # noqa: E402

# Seconds a cache stays good enough per event. A session start always fetches; prompt/turn edges
# only collapse duplicate firings; the tool edge fires after every tool call, so it is what keeps
# `used` current during one long turn — throttled to a couple of minutes, since the 5h window
# burns over tens of minutes and a fresher reading would only be polling.
MIN_AGE_S = {"session": 0, "prompt": 10, "stop": 10, "tool": 120}


def main(argv: list[str]) -> int:
    min_age_s = MIN_AGE_S.get(argv[1]) if len(argv) > 1 else None
    if min_age_s is None:
        return 0
    try:
        status = _quota.refresh(min_age_s)
    except Exception as error:  # noqa: BLE001 — a hook must never fail the session
        status = f"error: {error}"
    if status in ("failed", "no-token") or status.startswith("error"):
        print(f"pc-statusline quota: {status}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
