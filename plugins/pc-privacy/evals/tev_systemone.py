#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["presidio-analyzer", "presidio-anonymizer", "openpyxl", "fr-core-news-md @ https://github.com/explosion/spacy-models/releases/download/fr_core_news_md-3.8.0/fr_core_news_md-3.8.0-py3-none-any.whl"]
# ///
"""Compare the `pii_guard` block decision with Ollama's System One decision models.

    uv run --script tev_systemone.py FILE[:block|:pass] [...] [--models M ...] [--rules] [--threshold P]

Same files and labels as `tev_compare.py`, but through Ollama's `POST /v1/systemone` (Ollama 0.35+):
the file digest goes in `state` as a JSON object and the question is a boolean (`noul`) one whose
`instructions` are the question, optionally followed by the guard's own rules. The server builds the
prompt the models were trained on, and returns a probability (1 = yes, the file holds personal
data) instead of a letter. A file is blocked when that probability reaches `--threshold`.

The file name is never sent. Prints file names, labels, probabilities and timings only; the
sampled values go to the local Ollama server, on this machine.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
from pathlib import Path

import tev_compare
from tev_compare import (
    QUESTION,
    RULES,
    TEXT_LINES,
    VALUE_MAX_CHARS,
    VALUES_PER_COLUMN,
    anonymize,
)

# Marge sous la limite de 2050 tokens du serveur : noms de colonnes et valeurs cassent en tokens
# courts (~3 caractères), et les instructions avec règles en prennent environ 150.
STATE_BUDGET_CHARS = 4000
OLLAMA_HOST = "localhost"
OLLAMA_PORT = 11434
DEFAULT_MODELS = ("tev1:0.8b", "tev1", "nimble")


def _call(path: str, payload: dict | None = None) -> dict:
    """GET (no payload) or POST (JSON payload) against the local Ollama server.

    Args:
        path: URL path, starting with a slash.
        payload: JSON body for a POST; None for a GET.

    Returns:
        The decoded JSON response.
    """
    conn = http.client.HTTPConnection(OLLAMA_HOST, OLLAMA_PORT, timeout=300)
    try:
        conn.request(
            "GET" if payload is None else "POST",
            path,
            body=None if payload is None else json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        return json.load(conn.getresponse())
    finally:
        conn.close()


def _state(path: Path, fmt: str) -> tuple[dict, str]:
    """The `state` sent to the model: format, column names and a few sampled values.

    The server rejects a prompt over 2050 tokens and never truncates it, so a wide file is
    thinned: columns are kept at a regular interval, not the first ones, because a hook cannot know
    where the information sits.

    Args:
        path: The file.
        fmt: Its format, as decided by `anonymize.detect_format`.

    Returns:
        The JSON-serializable state, and a coverage note ("34/116 columns").
    """
    if fmt == "text":
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[
            :TEXT_LINES
        ]
        return {
            "format": fmt,
            "lines": [x[:VALUE_MAX_CHARS] for x in lines],
        }, f"{len(lines)} lines"
    samples = {
        name: [v[:VALUE_MAX_CHARS] for v in values[:VALUES_PER_COLUMN]]
        for name, values in anonymize._column_samples(path, fmt).items()
    }
    names = list(samples)
    step = 1
    while (
        sum(len(n) + sum(map(len, samples[n])) for n in names[::step])
        > STATE_BUDGET_CHARS
    ):
        step += 1
    columns = {n: samples[n] for n in names[::step]}
    return {"format": fmt, "columns": columns}, f"{len(columns)}/{len(names)} columns"


def _ask(model: str, state: dict, rules: bool) -> float:
    """Ask the boolean question about one file.

    Args:
        model: An Ollama model name serving System One.
        state: The file `state`.
        rules: Append the guard's own rules to the instructions.

    Returns:
        The probability that the file holds personal data that must be anonymized.

    Raises:
        RuntimeError: If the server answers with an error (a prompt over its token limit).
    """
    instructions = f"{QUESTION} {RULES}" if rules else QUESTION
    answer = _call(
        "/v1/systemone",
        {
            "model": model,
            "state": state,
            "questions": {"pii": {"type": "noul", "instructions": instructions}},
        },
    )
    if "answers" not in answer:
        raise RuntimeError(f"{model}: {json.dumps(answer)[:300]}")
    return float(answer["answers"]["pii"]["noul"])


def _run_model(
    model: str, states: dict[Path, dict], rules: bool
) -> tuple[dict[Path, float], float]:
    """Ask one model about every file, a first warm-up call excluded from the timing.

    Args:
        model: An Ollama model name serving System One.
        states: File -> state.
        rules: Append the guard's own rules to the instructions.

    Returns:
        (file -> probability) and the mean ms per file.
    """
    _ask(model, next(iter(states.values())), rules)
    t0 = time.perf_counter()
    probs = {path: _ask(model, state, rules) for path, state in states.items()}
    return probs, (time.perf_counter() - t0) * 1000 / len(states)


def main() -> int:
    """Judge each file with the guard and with every System One model, then print the comparison.

    Returns:
        0 on success, 2 when Ollama, a file or a model is unusable.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("files", nargs="+")
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument(
        "--rules", action="store_true", help="also run with the guard's rules"
    )
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()

    try:
        installed = {m["name"] for m in _call("/api/tags")["models"]}
    except OSError as e:
        print(f"Ollama is not reachable on {OLLAMA_HOST}:{OLLAMA_PORT}: {e}")
        return 2
    models = [m for m in args.models if m in installed or f"{m}:latest" in installed]
    for m in set(args.models) - set(models):
        print(f"skipped {m}: not pulled")
    cases = [tev_compare._parse_case(a) for a in args.files]
    formats = {c.path: anonymize.detect_format(c.path) for c in cases}
    cases = [c for c in cases if formats[c.path] not in anonymize._SKIP_FORMATS]
    if not models or not cases:
        print("no usable model, or every file is config, code or binary")
        return 2
    built = {c.path: _state(c.path, formats[c.path]) for c in cases}
    states = {path: state for path, (state, _) in built.items()}
    for path, (_, coverage) in built.items():
        print(f"{path.name}: {coverage} sent")

    verdicts = {"guard": {c.path: tev_compare._guard_decision(c.path) for c in cases}}
    probs: dict[str, dict[Path, float]] = {}
    for model in models:
        for rules in (False, True) if args.rules else (False,):
            key = model + ("+rules" if rules else "")
            probs[key], ms = _run_model(model, states, rules)
            verdicts[key] = {
                p: "block" if v >= args.threshold else "pass"
                for p, v in probs[key].items()
            }
            print(f"{key}: {ms:.0f} ms/file")

    names = list(probs)
    print(
        f"\n{'file':<48} {'label':<6} {'guard':<6} "
        + " ".join(n.ljust(16) for n in names)
    )
    for c in cases:
        cells = " ".join(
            f"{probs[n][c.path]:.2f} {verdicts[n][c.path]}".ljust(16) for n in names
        )
        print(
            f"{c.path.name:<48} {(c.expect or '-'):<6} {verdicts['guard'][c.path]:<6} {cells}"
        )
    if any(c.expect for c in cases):
        print()
        for n in ["guard", *names]:
            print(f"{n}: {tev_compare._errors(verdicts[n], cases)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
