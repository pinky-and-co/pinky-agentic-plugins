#!/usr/bin/env python3
"""Run the review cases through the pool's own call path (`run_pool`) on chosen pool entries.

Usage: run.py <entry-name>... [--timeout S]
Writes results/<entry>.json — one record per case: answer, seconds, per-defect hit (keyword
match, to be read by a human), and the answer length.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent / "mcp"))
import model_adversarial as ma  # noqa: E402

TASK = (
    "Review this diff for correctness bugs and unsafe behaviour. List each finding with a "
    "severity (High/Medium/Low), the line it concerns and why it is wrong. Say plainly if you "
    "find nothing wrong. Intent of the change: {intent}\n\n--- DIFF ---\n{diff}"
)


def main(argv: list[str]) -> int:
    """Run every case on every named entry, sequentially per entry."""
    timeout = 420
    names = [a for a in argv if not a.startswith("--")]
    if "--timeout" in argv:
        timeout = int(argv[argv.index("--timeout") + 1])
        names = [n for n in names if n != str(timeout)]
    pool = {e["name"]: e for e in ma.load_pool()}
    cases = json.loads((HERE / "cases.json").read_text())
    (HERE / "results").mkdir(exist_ok=True)
    for name in names:
        entry = pool[name]
        records = []
        for case in cases:
            diff = (HERE / "cases" / case["file"]).read_text()
            prompt = ma.build_prompt(TASK.format(intent=case["intent"], diff=diff), [])
            t = time.time()
            res = ma.run_pool([entry], prompt, timeout=timeout)
            secs = round(time.time() - t, 1)
            answer = (
                (res.result or "")
                if res.entry is not None
                else f"[no answer] {res.result}"
            )
            hits = {
                k: bool(re.search(rx, answer, re.I))
                for k, rx in case["defects"].items()
            }
            records.append(
                {
                    "case": case["id"],
                    "clean": case["clean"],
                    "secs": secs,
                    "answered": res.entry is not None,
                    "hits": hits,
                    "answer": answer,
                }
            )
            print(
                f"{name} {case['id']} {secs}s answered={res.entry is not None} hits={hits}",
                flush=True,
            )
        (HERE / "results" / f"{name}.json").write_text(
            json.dumps(records, indent=1, ensure_ascii=False)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
