"""Anti-bloat invariant over plugin hooks (incident 2026-07-04).

A hook that persists a MANAGED BLOCK into a file (marked by a `BLOCK_START` constant) must build
that block as a pure function of state — no wall-clock, no nonce. Otherwise the block differs every
turn, the hook rewrites the file, and because that file may already be in the agent's context the
harness re-injects it whole -> pure token waste (exactly how the auto-handoff timestamp leaked
~1K/turn).

Generic: it scans every plugin hook, protecting future ones, not just the one already fixed.
Plus a total-function check on the status-line entry point (an error there blanks the whole bar).
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
STATUSLINE_SCRIPTS = REPO / "plugins" / "pc-statusline" / "scripts"
sys.path.insert(0, str(STATUSLINE_SCRIPTS))

_WALLCLOCK = re.compile(
    r"\b(?:datetime\.now|datetime\.utcnow|time\.time|time\.monotonic)\s*\("
)


def test_managed_block_hooks_have_no_wallclock():
    offenders = []
    for f in sorted(REPO.glob("plugins/*/hooks/*.py")):
        src = f.read_text(encoding="utf-8")
        if "BLOCK_START" not in src:
            continue  # not a managed-block writer -> the invariant does not apply
        if _WALLCLOCK.search(src):
            offenders.append(str(f.relative_to(REPO)))
    assert not offenders, (
        "managed-block hooks must build their block from state only (no wall-clock) or they churn "
        f"the file and force context re-injection: {offenders}"
    )


def test_statusline_is_total_and_never_raises():
    import statusline as sl

    lines = sl.build_lines({})
    assert (
        lines and lines[0].strip()
    )  # missing everything -> still a non-empty skeleton
    assert sl._count_subagents("/nope/nope.jsonl", "sess") in (
        None,
        0,
    )  # unreadable transcript -> degrades, no crash
