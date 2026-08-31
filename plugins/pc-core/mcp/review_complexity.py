#!/usr/bin/env python3
"""Deterministic review-complexity signal — pick a model tier from a diff, no model in the loop.

Trimmed from pinky-bootstrap's ``tools/pr-analyzer.py`` (itself adapted from
``awesome-skills/code-review-skill``): kept the size + risk-factor read that a routing decision
needs, dropped the human-facing parts (estimated review time, prose suggestions, the
pretty-printer).

Used by the ``adversarial`` MCP tool to route a review to the cheapest model tier that can still
handle it — a small mechanical diff to a haiku-tier pool entry, a large or risky one to opus-tier.
The thresholds in ``tier_for`` are a first cut, meant to be tuned against real runs (story
``PLUGIN_MIGRATION_SPEC.md`` § Items ouverts).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# substrings in a changed file's path that mark it security-sensitive — a diff touching one of
# these is never routed below opus-tier, however small
_SECURITY_MARKERS = (
    ".env",
    "auth",
    "secur",
    "password",
    "token",
    "secret",
    "crypto",
    "permission",
)

_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__)/"
    r"|(^|/)test_[^/]*\.py$"
    r"|_test\.py$"
    r"|\.(test|spec)\.[jt]sx?$"
)
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")

# tier_for thresholds — a first cut, meant to be tuned against real runs (story § Items ouverts).
# All comparisons are strict ``>``.
_NO_TEST_MIN_LINES = 50  # below this a testless change is too small to flag
_UNDERTESTED_OPUS_LINES = 150  # testless AND bigger than this → top tier
_OPUS_LINES = 400
_SONNET_LINES = 80
_SONNET_FILES = 5


@dataclass
class DiffStats:
    """What a routing decision reads off a diff — counts plus named risk factors."""

    files: int
    changed_lines: int
    test_lines: int
    risk_factors: list[str] = field(default_factory=list)


def _iter_file_blocks(diff: str) -> list[tuple[str, int]]:
    """Each changed file as ``(path, changed_line_count)`` — added + removed content lines."""
    blocks: list[tuple[str, int]] = []
    path: str | None = None
    count = 0
    for line in diff.splitlines():
        header = _DIFF_HEADER_RE.match(line)
        if header:
            if path is not None:
                blocks.append((path, count))
            path = header.group("b")
            count = 0
        elif path is not None:
            if line.startswith(("+++", "---")):
                continue
            if line.startswith(("+", "-")):
                count += 1
    if path is not None:
        blocks.append((path, count))
    return blocks


def analyze(diff: str) -> DiffStats:
    """Parse a unified diff into the counts + risk factors ``tier_for`` decides on."""
    blocks = _iter_file_blocks(diff)
    changed = sum(n for _, n in blocks)
    test_lines = sum(n for p, n in blocks if _TEST_PATH_RE.search(p))

    risks: list[str] = []
    if changed > 400:
        risks.append("large-diff")
    if test_lines == 0 and changed > _NO_TEST_MIN_LINES:
        risks.append("no-test-changes")
    for p, _ in blocks:
        if any(marker in p.lower() for marker in _SECURITY_MARKERS):
            risks.append(f"security-sensitive:{p}")
            break
    for p, _ in blocks:
        if "migration" in p.lower() or p.lower().endswith(".sql"):
            risks.append("db-migration")
            break

    return DiffStats(
        files=len(blocks),
        changed_lines=changed,
        test_lines=test_lines,
        risk_factors=risks,
    )


def tier_for(diff: str) -> str:
    """A ``capacity_tier`` string (``haiku`` / ``sonnet`` / ``opus``) for reviewing this diff.

    First cut, tunable: a security-sensitive path or a large/undertested change goes to the top
    tier; a moderate change to the middle; a small mechanical one to the cheapest.
    """
    stats = analyze(diff)
    security = any(r.startswith("security-sensitive:") for r in stats.risk_factors)
    undertested_and_big = (
        "no-test-changes" in stats.risk_factors
        and stats.changed_lines > _UNDERTESTED_OPUS_LINES
    )
    if stats.changed_lines > _OPUS_LINES or security or undertested_and_big:
        return "opus"
    if stats.changed_lines > _SONNET_LINES or stats.files > _SONNET_FILES:
        return "sonnet"
    return "haiku"
