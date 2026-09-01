#!/usr/bin/env python3
"""Reconcile Step 0 — framing oracle. Score ``reconcile.PHASE1_TASK`` against known-answer cases.

**Why this exists.** Twice (2026-07-16, 2026-07-17) a session retuned the phase-1 prompt by eye on
two repos with no ground truth, and the second attempt broke the tool: a "report only what breaks a
promise / when in doubt stay silent" framing made the model rubber-stamp a repo carrying 3 stubs
marked OK (``VERDICT: 0 diverge, 0 manquant``, no analysis). The rule that came out of it — **no
change to the prompt ships without this oracle passing** — needs the oracle to exist. This is it.

Two frozen cases, deliberately opposite (``fixtures/``):

- **recall_broken_promises** — 3 real provider stubs marked delivered, plus a complete resource as
  the contrast reference. A framing that misses any of them has rubber-stamped. The hard one.
- **precision_clean_table** — a real, hand-verified-honest capability table + module. The correct
  output is near-silence; the anchor is that ``purge_expired`` (a row that already exists) must NOT
  be demanded as missing — the known false positive.

Scoring mirrors hand verification: is the object's name present in the report body? The framing is
told to emit only problem lines, so a mention = a flag. Crude, but it is exactly the check a human
does at triage, and it is stable across prose phrasings.

Transport (rewired from the deprecated Mistral-only call): ``--transport offline`` runs the local
Ollama model via ``model_offline`` (zero egress, the primary reconcile path); ``--transport panel``
runs a pool sonnet/opus model via ``model_panel`` (the cloud fallback). ``--model`` overrides the
pick on either path. NOT a pytest: it calls a live model and scores a probabilistic output — the
scorer LOGIC is unit-tested in ``tests/test_reconcile_oracle.py`` with a fake.

Usage:
    reconcile_oracle.py --transport offline                 # score every case on local Ollama
    reconcile_oracle.py --transport panel --model mistral-medium-3.5
    reconcile_oracle.py --transport offline --show recall_broken_promises   # dump one full answer
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "pc-core" / "mcp"))

import model_adversarial
import model_offline
import model_panel
import reconcile as rc

HERE = Path(__file__).resolve().parent
CASES_DIR = HERE / "fixtures"
_DOC_REL = "docs/explanation/2-features.md"


@dataclass(frozen=True)
class Case:
    """One known-answer fixture: its table, its sources, and the objects the report must / must not name."""

    name: str
    table_md: str
    sources: dict[str, str]
    must_flag: list[str]
    must_not_flag: list[str]


def load_cases() -> list[Case]:
    """Load every fixture directory under ``fixtures/`` into a `Case`.

    Returns:
        The cases in directory-name order.
    """
    cases: list[Case] = []
    for d in sorted(p for p in CASES_DIR.iterdir() if p.is_dir()):
        oracle = json.loads((d / "oracle.json").read_text(encoding="utf-8"))
        sources = {
            str(f.relative_to(d)): f.read_text(encoding="utf-8")
            for f in sorted((d / "src").rglob("*.py"))
        }
        cases.append(
            Case(
                name=d.name,
                table_md=(d / "features_table.md").read_text(encoding="utf-8"),
                sources=sources,
                must_flag=oracle.get("must_flag", []),
                must_not_flag=oracle.get("must_not_flag", []),
            )
        )
    return cases


def flagged(answer: str, obj: str) -> bool:
    """Did the report name this object as a finding? The framing emits only problem lines, so a
    mention = a flag.

    Whole-token match, not substring: ``database`` (the object) must not match inside
    ``database-scoped`` (a description word) — that false match made the recall case wrongly fail on
    2026-07-17. A token is bounded by anything other than a word char or hyphen, so neither a
    hyphenated compound (``database-scoped``) nor a longer identifier (``api_integration_v2``) counts.

    Scored on the body only — never the trailing ``VERDICT:`` line, which restates counts and could
    echo a name from the task instructions rather than a real finding.

    Args:
        answer: the model's full reply.
        obj: the object name to look for.

    Returns:
        Whether the object is named in the report body.
    """
    body = answer.split("VERDICT:")[0].lower()
    return re.search(rf"(?<![\w-]){re.escape(obj.lower())}(?![\w-])", body) is not None


@dataclass(frozen=True)
class Score:
    """The outcome of scoring one case: which required objects were caught, missed, or wrongly flagged."""

    name: str
    recall_hits: list[str]
    recall_misses: list[str]
    precision_violations: list[str]

    @property
    def passed(self) -> bool:
        """True when nothing required was missed and nothing forbidden was flagged.

        Returns:
            Whether the case passed.
        """
        return not self.recall_misses and not self.precision_violations


def score_case(case: Case, answer: str) -> Score:
    """Score one model answer against a case's oracle.

    Args:
        case: the fixture with its must/must-not lists.
        answer: the model's reply for this case.

    Returns:
        The `Score` for this case.
    """
    hits = [o for o in case.must_flag if flagged(answer, o)]
    misses = [o for o in case.must_flag if o not in hits]
    violations = [o for o in case.must_not_flag if flagged(answer, o)]
    return Score(case.name, hits, misses, violations)


# The brief builders the oracle guards. `signature` is the one the skill actually ships (bounded,
# stub-flagged); `all` (every full body) and `selective` (index + referenced bodies) are kept as
# comparison baselines — a framing change is scored against all three.
BRIEF_BUILDERS: dict[str, Callable[[Case], str]] = {
    "signature": lambda case: rc.build_signature_brief(
        _DOC_REL, case.table_md, case.sources
    ),
    "all": lambda case: rc.build_phase1_brief(_DOC_REL, case.table_md, case.sources),
    "selective": lambda case: rc.build_selective_brief(
        _DOC_REL, case.table_md, case.sources
    ),
}


def run_case(case: Case, call: Callable[[str], str], brief_mode: str = "all") -> str:
    """Build the brief for a case and send ``PHASE1_TASK`` + brief through ``call``.

    Args:
        case: the fixture to run.
        call: a ``prompt -> answer`` function (a live transport or a test fake).
        brief_mode: which entry of `BRIEF_BUILDERS` to use.

    Returns:
        The model's raw answer.
    """
    brief = BRIEF_BUILDERS[brief_mode](case)
    return call(f"{rc.PHASE1_TASK}\n\n{brief}")


def _offline_call(model: str | None) -> Callable[[str], str]:
    """A ``prompt -> answer`` function backed by the local Ollama model (``model_offline``).

    Args:
        model: an explicit Ollama model name, or None for the ``model_offline`` default.

    Returns:
        The call function; exits the process if Ollama is not reachable.
    """
    url = model_offline.resolve_url()
    resolved = model_offline.resolve_model(model)
    hint = model_offline.preflight(url, resolved)
    if hint:
        sys.exit(f"[oracle] offline transport unavailable — {hint}")
    num_predict = model_offline.resolve_max_tokens(None)

    def call(prompt: str) -> str:
        r = model_offline.call_ollama(
            url,
            resolved,
            prompt,
            model_offline.num_ctx_for(prompt, num_predict),
            num_predict,
        )
        return r.get("response") or ""

    return call


def _panel_call(model: str | None) -> Callable[[str], str]:
    """A ``prompt -> answer`` function backed by one pool sonnet/opus model (``model_panel``).

    Args:
        model: an explicit pool entry name, or None to take the strongest eligible family.

    Returns:
        The call function; exits the process if the pool has no eligible model.
    """
    entries = model_panel.select_for_panel(
        model_adversarial.load_pool(), 1, [model] if model else None, None
    )
    if not entries:
        sys.exit("[oracle] panel transport — no eligible sonnet/opus model in the pool")

    def call(prompt: str) -> str:
        response = model_panel.run_panel(entries, [prompt])
        if not response.answers:
            raise RuntimeError(f"panel call failed: {response.skipped}")
        return response.answers[0].text

    return call


_TRANSPORTS: dict[str, Callable[[str | None], Callable[[str], str]]] = {
    "offline": _offline_call,
    "panel": _panel_call,
}


def main(argv: list[str] | None = None) -> int:
    """Score ``PHASE1_TASK`` against every fixture and print a scorecard.

    Args:
        argv: argument vector, or None to read ``sys.argv``.

    Returns:
        0 when every case passed on every brief, 1 otherwise.
    """
    ap = argparse.ArgumentParser(
        description="Score the reconcile prompt against the oracle cases."
    )
    ap.add_argument("--transport", choices=list(_TRANSPORTS), default="offline")
    ap.add_argument("--model", help="override the model pick on the chosen transport")
    ap.add_argument("--show", help="dump the full model answer for one case name")
    ap.add_argument(
        "--brief",
        choices=[*BRIEF_BUILDERS, "both"],
        default="both",
        help="which brief to score: all-source, selective, or both (default)",
    )
    args = ap.parse_args(argv)

    cases = load_cases()
    call = _TRANSPORTS[args.transport](args.model)
    modes = list(BRIEF_BUILDERS) if args.brief == "both" else [args.brief]

    if args.show:
        case = next((c for c in cases if c.name == args.show), None)
        if case is None:
            sys.exit(
                f"[oracle] no case named {args.show} (have: {[c.name for c in cases]})"
            )
        print(run_case(case, call, modes[0]))
        return 0

    all_pass = True
    for mode in modes:
        print(f"\n### transport = {args.transport} · brief = {mode}")
        for case in cases:
            s = score_case(case, run_case(case, call, mode))
            all_pass &= s.passed
            print(f"[{'PASS' if s.passed else 'FAIL'}] {case.name}")
            if case.must_flag:
                line = (
                    f"    recall   : {len(s.recall_hits)}/{len(case.must_flag)} caught"
                )
                if s.recall_misses:
                    line += f" — MISSED {s.recall_misses}"
                print(line)
            if case.must_not_flag:
                v = s.precision_violations
                print(
                    f"    precision: {'clean' if not v else f'FLAGGED {v} (should not)'}"
                )
    print("\nOVERALL:", "PASS" if all_pass else "FAIL")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
