#!/usr/bin/env python3
"""Run the review cases on one model with no repository checkout — standard library only.

Needs `pi` on PATH (the same CLI the pool uses), or an OpenAI-compatible endpoint.

    python3 standalone.py --model mlx/<org>/<model-id> --thinking off --cases A D
    python3 standalone.py --openai-url http://127.0.0.1:8000/v1 --model <served-model-name>

Writes results-<label>.json next to this file: per case the prompt size, the seconds, and the
answer. Cases A and D are the ones the pool models missed (the subtle defects).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
NO_TOOLS = (
    "You have no tools. The diff is below in full: answer from it alone, do not try to open "
    "files, run commands or emit tool calls. Reply with your findings and a verdict."
)


def build_prompt(case: dict[str, object]) -> str:
    """Methodology, the no-tool instruction, the intent and the diff — the tool's own shape."""
    methodology = (HERE / "methodology.md").read_text(encoding="utf-8")
    diff = (HERE / "cases" / str(case["file"])).read_text(encoding="utf-8")
    return (
        f"--- TASK ---\n{methodology}\n\n{NO_TOOLS}\n\nReview this diff. Intent of the change: "
        f"{case['intent']}\n\n--- DIFF ---\n{diff}"
    )


def ask_pi(prompt: str, model: str, thinking: str | None, timeout: int) -> str:
    """One answer through `pi -p`, no tools, stdin closed (pi reads an inherited stdin)."""
    cmd = ["pi", "-p", "--model", model, "--no-tools"]
    if thinking:
        cmd += ["--thinking", thinking]
    cmd += ["--", prompt]
    done = subprocess.run(
        cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
    )
    return (
        done.stdout
        if done.returncode == 0
        else f"[exit {done.returncode}] {done.stderr[-500:]}"
    )


def ask_openai(prompt: str, url: str, model: str, timeout: int) -> str:
    """One answer through an OpenAI-compatible `/chat/completions` endpoint."""
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": prompt}]}
    ).encode()
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"--openai-url must be http(s): {url!r}")
    req = urllib.request.Request(  # noqa: S310 — scheme checked just above
        url.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return json.loads(resp.read())["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as exc:
        return f"[http {exc.code}] {exc.read().decode(errors='replace')[:600]}"


def main() -> int:
    """Run the chosen cases and write the results file."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True)
    ap.add_argument("--thinking", default=None)
    ap.add_argument("--openai-url", default=None)
    ap.add_argument("--cases", nargs="*", default=None)
    ap.add_argument("--timeout", type=int, default=1200)
    args = ap.parse_args()
    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    chosen = [c for c in cases if not args.cases or c["id"] in args.cases]
    out: dict[str, dict[str, object]] = {}
    for case in chosen:
        prompt = build_prompt(case)
        t = time.time()
        try:
            if args.openai_url:
                answer = ask_openai(prompt, args.openai_url, args.model, args.timeout)
            else:
                answer = ask_pi(prompt, args.model, args.thinking, args.timeout)
        except Exception as exc:  # noqa: BLE001 — a probe records any failure and moves on
            answer = f"[error] {exc!r}"
        secs = round(time.time() - t, 1)
        out[case["id"]] = {"chars": len(prompt), "secs": secs, "answer": answer}
        print(f"{case['id']} {len(prompt)} chars {secs}s", flush=True)
        print(answer[:600].replace("\n\n", "\n"), "\n", flush=True)
    label = args.model.replace("/", "_").replace(":", "_")
    (HERE / f"results-{label}.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
