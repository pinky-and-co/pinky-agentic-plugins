#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["presidio-analyzer", "presidio-anonymizer", "openpyxl", "fr-core-news-md @ https://github.com/explosion/spacy-models/releases/download/fr_core_news_md-3.8.0/fr_core_news_md-3.8.0-py3-none-any.whl"]
# ///
"""Compare the `pii_guard` block decision with oMLX System One decision models.

    uv run --script tev_systemone.py FILE[:block|:pass] [...] [--models M ...] [--rules] [--threshold P]

Same files and labels as `tev_compare.py`, but through oMLX `POST /v1/systemone`: the file
state goes in `state` as a JSON object and the question is a boolean (`noul`) one whose
`instructions` are the question, optionally followed by the guard's own rules. The server
builds the prompt the models were trained on, and returns a probability (1 = yes, the file
holds personal data) instead of a letter. A file is blocked when that probability reaches
`--threshold`.

The server address comes from `ONE_DECISION_URL` (default `http://127.0.0.1:11234`), on a
loopback host only, like the gate itself. The file name is never sent. Prints file names,
labels, probabilities and timings only; the sampled values go to the local oMLX server, on
this machine.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "hooks"))
import tev_compare  # noqa: E402
import decision_gate  # noqa: E402
from tev_compare import (  # noqa: E402
    QUESTION,
    RULES,
    TEXT_LINES,
    VALUE_MAX_CHARS,
    VALUES_PER_COLUMN,
    anonymize,
)

DEFAULT_MODELS = ("APUS-OpenJev-v1-4B-MLX-4bit", "APUS-OpenJev-v1-4B-MLX-8bit")


def _base() -> urllib.parse.SplitResult | None:
    """The oMLX base URL, as a loopback http(s) split or None when it is not one.

    Returns:
        The parsed base URL from `ONE_DECISION_URL` or the default, or None when the host is
        not loopback.
    """
    raw = os.environ.get(decision_gate.URL_ENV) or decision_gate.DEFAULT_URL
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return None
    if not decision_gate._is_loopback(raw):
        return None
    return parts


def _call(
    parts: urllib.parse.SplitResult, path: str, payload: dict | None = None
) -> dict:
    """GET (no payload) or POST (JSON payload) against the oMLX server.

    Args:
        parts: The parsed base URL.
        path: URL path, starting with a slash.
        payload: JSON body for a POST; None for a GET.

    Returns:
        The decoded JSON response.
    """
    connection: http.client.HTTPConnection
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(
            parts.hostname or "", parts.port or 443, timeout=300
        )
    else:
        connection = http.client.HTTPConnection(
            parts.hostname or "", parts.port or 80, timeout=300
        )
    try:
        connection.request(
            "GET" if payload is None else "POST",
            path,
            body=None if payload is None else json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        return json.load(connection.getresponse())
    finally:
        connection.close()


def _state(path: Path, fmt: str) -> tuple[dict, str]:
    """The `state` sent to the model: format, column names and a few sampled values.

    Every column is sent, without thinning: the gate never thins either, and the models'
    window fits a full digest.

    Args:
        path: The file.
        fmt: Its format, as decided by `anonymize.detect_format`.

    Returns:
        The JSON-serializable state, and a coverage note ("34/34 columns").
    """
    if fmt == "text":
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[
            :TEXT_LINES
        ]
        return {
            "format": fmt,
            "lines": [x[:VALUE_MAX_CHARS] for x in lines],
        }, f"{len(lines)} lines"
    columns = {
        name: [v[:VALUE_MAX_CHARS] for v in values[:VALUES_PER_COLUMN]]
        for name, values in anonymize._column_samples(path, fmt).items()
    }
    return {"format": fmt, "columns": columns}, f"{len(columns)}/{len(columns)} columns"


def _ask(
    model: str, state: dict, rules: bool, parts: urllib.parse.SplitResult
) -> float:
    """Ask the boolean question about one file.

    Args:
        model: An oMLX model id serving System One.
        state: The file `state`.
        rules: Append the guard's own rules to the instructions.
        parts: The parsed base URL.

    Returns:
        The probability that the file holds personal data that must be anonymized.

    Raises:
        RuntimeError: If the server answers with an error (a prompt over its token limit).
    """
    instructions = f"{QUESTION} {RULES}" if rules else QUESTION
    answer = _call(
        parts,
        "/v1/systemone",
        {
            "model": model,
            "state": state,
            "questions": {"pii": {"type": "noul", "instructions": instructions}},
            "truncate": False,
        },
    )
    if "answers" not in answer:
        raise RuntimeError(f"{model}: {json.dumps(answer)[:300]}")
    return float(answer["answers"]["pii"]["noul"])


def _run_model(
    model: str,
    states: dict[Path, dict],
    rules: bool,
    parts: urllib.parse.SplitResult,
) -> tuple[dict[Path, float], float]:
    """Ask one model about every file, a first warm-up call excluded from the timing.

    Args:
        model: An oMLX model id serving System One.
        states: File -> state.
        rules: Append the guard's own rules to the instructions.
        parts: The parsed base URL.

    Returns:
        (file -> probability) and the mean ms per file.
    """
    _ask(model, next(iter(states.values())), rules, parts)
    t0 = time.perf_counter()
    probs = {path: _ask(model, state, rules, parts) for path, state in states.items()}
    return probs, (time.perf_counter() - t0) * 1000 / len(states)


def main() -> int:
    """Judge each file with the guard and with every System One model, then print the comparison.

    Returns:
        0 on success, 2 when oMLX, a file or a model is unusable.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("files", nargs="+")
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument(
        "--rules", action="store_true", help="also run with the guard's rules"
    )
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()

    parts = _base()
    if parts is None:
        print(
            f"{os.environ.get(decision_gate.URL_ENV) or decision_gate.DEFAULT_URL} is not "
            "a loopback http(s) URL: the eval sends file values, it only talks to a local "
            "server"
        )
        return 2
    try:
        installed = {m["id"] for m in _call(parts, "/v1/models")["data"]}
    except (OSError, KeyError, ValueError) as e:
        print(f"oMLX is not reachable on {parts.hostname}:{parts.port or 80}: {e}")
        return 2
    models = [m for m in args.models if m in installed]
    for m in set(args.models) - set(models):
        print(f"skipped {m}: not served")
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
            probs[key], ms = _run_model(model, states, rules, parts)
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
