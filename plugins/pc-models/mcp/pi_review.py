#!/usr/bin/env python3
"""Review a kanban card's `## Done when`, point by point, against the real repo — via `pi`, a
cheap standalone alternative to `adversarial`/`reviewer` when those read a pasted report instead of
the repo itself. Not wired into `finishing-a-card.md`'s review steps — callable on its own, the
loop integration is a separate, deliberately unstarted subject.

Takes a card's absolute path and a repo's absolute path, and renders one structured finding per
`### N. <title>` point of the card's `## Done when` section (the whole section as a single point
when it carries no such subheadings). Never edits, commits, or moves the card — findings only; the
triage stays with the orchestrator that invoked this script.

Model choice: `model_adversarial.select_for_delegation(pool, "haiku")`, filtered to `kind == "cli"`
— the entries `pi` can call with its `read` tool; an `offline` entry would review a pasted prompt
blind, the exact failure this script exists to avoid. `local` pool entries (MLX-SERVE) sort first
automatically. Every call passes `REVIEW_TOOLS` (`read` only) — the containment this script promises
is fixed here, never read from pool config.

Runtime: Python 3.12+, stdlib only, imports `model_adversarial` for pool loading/dispatch.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import model_adversarial as ma  # noqa: E402

DEFAULT_TIMEOUT = ma.RUN_TIMEOUT

_DONE_WHEN_RE = re.compile(
    r"^##\s+Done when\b.*?\n(.*?)(?=\n##\s|\Z)", re.MULTILINE | re.DOTALL
)
_POINT_RE = re.compile(r"^###\s+\d+\.\s+(.+)$", re.MULTILINE)

_PROMPT_TEMPLATE = """You are a read-only reviewer. The repo lives at {repo}.

Verify EXACTLY this one point of a card's "Done when", by reading the real files of the repo
(absolute paths) — never a summary, never a guess:

{title}
{body}

Answer in exactly two lines:
STATUS: OK or GAP
JUSTIFICATION: one sentence, naming the exact file and line that prove your verdict.
"""


def extract_done_when(card_text: str) -> str:
    """The `## Done when` section's body, or "" when the card has no such heading."""
    m = _DONE_WHEN_RE.search(card_text)
    return m.group(1).strip() if m else ""


def split_points(done_when: str) -> list[tuple[str, str]]:
    """`(title, body)` per `### N. <title>` subheading, or the whole section as one point when
    the card's `## Done when` carries no such subheadings."""
    matches = list(_POINT_RE.finditer(done_when))
    if not matches:
        return [("Done when", done_when.strip())] if done_when.strip() else []
    points = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(done_when)
        points.append((m.group(1).strip(), done_when[start:end].strip()))
    return points


def build_prompt(repo: str, title: str, body: str) -> str:
    """The read-only review prompt for one `## Done when` point."""
    return _PROMPT_TEMPLATE.format(repo=repo, title=title, body=body)


REVIEW_TOOLS = ["read"]


def review_candidates(pool: list[dict]) -> list[dict]:
    """Cheap candidates `pi` can call with a read tool: `kind == "cli"` only, local-first."""
    return [
        e
        for e in ma.select_for_delegation(pool, "haiku", tools=REVIEW_TOOLS)
        if e["kind"] == "cli"
    ]


def review_point(
    entries: list[dict], repo: str, title: str, body: str, timeout: int | None
) -> dict:
    """One finding: `{point, status, detail, model}` — `status` one of `OK`/`GAP`/`ERROR`.

    `ERROR` covers both a critical `pi` failure and a fully exhausted pool — never a fabricated
    verdict.
    """
    prompt = build_prompt(repo, title, body)
    result = ma.run_pool(entries, prompt, timeout=timeout, tools=REVIEW_TOOLS)
    if result.entry is None:
        return {
            "point": title,
            "status": "ERROR",
            "detail": str(result.result),
            "model": None,
        }
    content = (result.result or "").strip()
    status_match = re.search(r"STATUS:\s*(OK|GAP)", content, re.IGNORECASE)
    status = status_match.group(1).upper() if status_match else "ERROR"
    detail = content if status_match else f"unparseable response: {content}"
    return {
        "point": title,
        "status": status,
        "detail": detail,
        "model": result.entry["name"],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Review a kanban card's Done when against a repo, via a rotating pi pool."
    )
    ap.add_argument("card", help="absolute path to the card's .md file")
    ap.add_argument("repo", help="absolute path to the repo to review")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = ap.parse_args(argv)

    try:
        card_text = Path(args.card).read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        print(json.dumps({"error": f"cannot read card: {e}"}))
        return 1
    points = split_points(extract_done_when(card_text))
    if not points:
        print(json.dumps({"error": "no '## Done when' section found in card"}))
        return 1

    pool = ma.load_pool()
    entries = review_candidates(pool)
    if not entries:
        print(json.dumps({"error": "no cli-kind candidate in the pool"}))
        return 1

    findings = [
        review_point(entries, args.repo, title, body, args.timeout)
        for title, body in points
    ]
    print(json.dumps(findings, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
