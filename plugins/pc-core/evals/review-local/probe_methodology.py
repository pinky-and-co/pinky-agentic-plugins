#!/usr/bin/env python3
"""Probe one pool entry on chosen cases with the tool's real prompt shape: the shared review
methodology, the task and the diff, plus an explicit no-tool instruction.

Usage: probe_methodology.py <entry-name> <case-id>...
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent / "mcp"))
import model_adversarial as ma  # noqa: E402

NO_TOOLS = (
    "You have no tools. The diff is below in full: answer from it alone, do not try to open "
    "files, run commands or emit tool calls. Reply with your findings and a verdict."
)


def main(argv: list[str]) -> int:
    """Run the named cases on the named entry and write results/<entry>-methodology.json."""
    name, ids = argv[0], argv[1:]
    entry = {e["name"]: e for e in ma.load_pool()}[name]
    cases = {c["id"]: c for c in json.loads((HERE / "cases.json").read_text())}
    methodology = ma.load_methodology(None)
    out: dict[str, dict[str, object]] = {}
    for cid in ids:
        case = cases[cid]
        diff = (HERE / "cases" / case["file"]).read_text()
        task = (
            f"{methodology}\n\n{NO_TOOLS}\n\nReview this diff. Intent of the change: "
            f"{case['intent']}\n\n--- DIFF ---\n{diff}"
        )
        prompt = ma.build_prompt(task, [])
        t = time.time()
        res = ma.run_pool([entry], prompt, timeout=900)
        secs = round(time.time() - t, 1)
        answer = (
            (res.result or "") if res.entry is not None else f"[no answer] {res.result}"
        )
        out[cid] = {
            "secs": secs,
            "chars": len(prompt),
            "answered": res.entry is not None,
            "answer": answer,
        }
        print(cid, len(prompt), "chars", secs, "s", flush=True)
        print(answer[:900].replace("\n\n", "\n"), "\n", flush=True)
    (HERE / "results" / f"{name}-methodology.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
