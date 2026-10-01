#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["presidio-analyzer[gliner]", "presidio-anonymizer", "openpyxl", "fr-core-news-md @ https://github.com/explosion/spacy-models/releases/download/fr_core_news_md-3.8.0/fr_core_news_md-3.8.0-py3-none-any.whl"]
# ///
"""Compare spaCy-only vs spaCy + GLiNER detection on real files: what each flags, and how long it takes.

    uv run --script gliner_compare.py FILE [FILE ...]

Prints column/entity names and timings only — never a value. The first GLiNER run downloads the
model (several hundred MB); the timings below exclude nothing, so run it twice and read the second.

What to look at:
  - columns flagged ONLY with GLiNER  -> what spaCy was missing (ALL-CAPS names, mostly)
  - columns flagged ONLY without it   -> GLiNER lost something: investigate before adopting
  - `analyzer build` and `detect` times -> the extra latency a Read hook would pay
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp"))
import anonymize


def run(label: str, files: list[str], gliner: bool) -> dict[tuple[str, str], str]:
    if gliner:
        os.environ["PC_PRIVACY_NER"] = "gliner"
    else:
        os.environ.pop("PC_PRIVACY_NER", None)
    t0 = time.perf_counter()
    anonymize._build_analyzer()
    t_build = time.perf_counter() - t0
    t0 = time.perf_counter()
    findings = anonymize.detect_findings(
        files
    )  # builds its own analyzer: time includes it
    t_detect = time.perf_counter() - t0
    print(
        f"\n[{label}] analyzer build {t_build:5.1f}s | detect_findings (incl. build) {t_detect:5.1f}s"
    )
    out = {}
    for f in findings:
        out[(f.file, f.column)] = f.reason
        print(f"   {f.file:<26} {f.column:<26} {f.reason}")
    if not findings:
        print("   (nothing flagged)")
    return out


def main() -> int:
    files = sys.argv[1:]
    if not files:
        print(__doc__)
        return 2
    base = run("spaCy only ", files, gliner=False)
    try:
        plus = run("spaCy+GLiNER", files, gliner=True)
    except anonymize.AnonymizeError as e:
        print(f"\n[spaCy+GLiNER] could not run: {e}")
        return 1
    only_gliner = sorted(set(plus) - set(base))
    only_base = sorted(set(base) - set(plus))
    print("\n== diff ==")
    print("only WITH GLiNER   :", only_gliner or "-")
    print("only WITHOUT GLiNER:", only_base or "-")
    return 0


if __name__ == "__main__":
    sys.exit(main())
