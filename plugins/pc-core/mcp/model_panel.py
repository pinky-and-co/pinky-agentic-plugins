#!/usr/bin/env python3
"""Panel runtime — dispatch one prompt PER SLOT to a different model family each, in parallel.

Called by the `panel` MCP tool (`mcp/pc_core_server.py`). Where `adversarial` runs a single contrarian
check and `offline` runs one local model, `panel` fans a batch of prompts out across several
*different* model families at once — `prompts[i]` to the i-th selected model — and returns every
answer, each labelled with the model that produced it. It exists for a council of advisor voices
(five lenses, five prompts, five models) and for any other multi-model fan-out. For the same
question to N models, repeat it in the batch. **No review posture is injected** (unlike
`adversarial`, which prepends the shared review methodology): each prompt is sent verbatim, because
a council's lenses are not all adversarial.

Dual-use: standalone CLI (below) and an importable library for the MCP server, which calls these
functions directly. Pool resolution and the `pi` call (`call_cli`, tools none by default) are reused
from `model_adversarial` — this module only adds the selection policy and the parallel dispatch.

Eligibility for a slot: `kind` is `cli` (never `offline` — that has its own zero-egress tool) and
`capacity_tier` is `sonnet` or `opus` by default (a `haiku`-tier model cannot hold an advisor
analysis, and mixing one in reads as model diversity when it is really a quality gap — see
`pinky-bootstrap/docs/adr/0006-council-advisors-cross-modele.md`). A caller that is not running a
council can pass an explicit `tiers` to `select_for_panel` to opt past that default — e.g. a small
deterministic model for a non-advisor task.

Usage:
    model_panel.py --task "<prompt>" [--task "<prompt>" ...] [--model NAME ...] [--tool NAME ...]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent))

import call_scope
import model_adversarial as ma

ADVISOR_TIERS = frozenset({"sonnet", "opus"})
DEFAULT_TIMEOUT = 180


class Answer(NamedTuple):
    """One model's reply in a panel run. `slot` is the 1-based index of the prompt it answered;
    `model` is the pool entry's name, `label` how the reply names its author (`answer_label`)."""

    slot: int
    model: str
    text: str
    label: str = ""


class Skip(NamedTuple):
    """A selected model that was tried and did not answer. `slot` is its prompt's 1-based index."""

    slot: int
    model: str
    reason: str


class PanelResponse(NamedTuple):
    """One panel run: the answers that came back, and the selected slots that did not."""

    answers: list[Answer]
    skipped: list[Skip]


class _SlotResult(NamedTuple):
    """Internal: one worker's outcome — exactly one of `answer` / `reason` is set."""

    entry: dict
    answer: str | None
    reason: str | None


def _eligible(entry: dict, tiers: frozenset[str] = ADVISOR_TIERS) -> bool:
    """True if this pool entry can fill a slot: an HTTP model whose tier is in `tiers`.

    `tiers` defaults to the sonnet/opus advisor set; a caller passing an explicit set (e.g. a
    non-advisor consumer that wants a small deterministic model) opts past that default. The
    `kind == "cli"` half is never relaxed — offline has its own zero-egress tool.
    """
    return entry.get("kind") == "cli" and entry.get("capacity_tier") in tiers


def _normalize_tiers(tiers: str | Iterable[str] | None) -> frozenset[str]:
    """Coerce the `tiers` argument to a frozenset; `None` means the default advisor set."""
    if tiers is None:
        return ADVISOR_TIERS
    if isinstance(tiers, str):
        return frozenset({tiers})
    return frozenset(tiers)


def _distinct_families(entries: list[dict], n: int) -> list[dict]:
    """Keep the first entry of each `provider`, in the given order, capped at `n`.

    One family = one voice: two models behind the same provider add no model diversity and, since
    `run_panel` fans out in parallel, would also hit that provider's rate limit at once.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        if entry["provider"] in seen:
            continue
        seen.add(entry["provider"])
        out.append(entry)
        if len(out) == n:
            break
    return out


def select_for_panel(
    pool: list[dict],
    n: int,
    models: list[str] | None = None,
    tiers: str | Iterable[str] | None = None,
    tools: list[str] | None = None,
) -> list[dict]:
    """Pick up to `n` eligible, family-distinct pool entries for a panel, deterministically.

    Both paths enforce one entry per `provider` (see `_distinct_families`) and drop `offline`
    entries plus any whose `capacity_tier` is outside `tiers` (see `_eligible`).

    `tools` given: an entry marked `tools: false` is dropped on both paths. Without `models`, the
    remembered provider (`ma.read_last_ok_provider`) is ranked first, after the tier and tools filters.

    `tiers` omitted: only sonnet/opus entries are eligible (the advisor default). `tiers` given (a
    tier name or an iterable of them): exactly those tiers are eligible — an explicit opt-out of
    the advisor filter for a non-advisor caller (e.g. a small deterministic model). It applies on
    both selection paths.

    `models` given: the named entries in that order, minus the ineligible and the family duplicates
    (an unknown or dropped name is silent here — `run_panel`'s skipped-reason channel is for entries
    that were selected and then failed, not for selection choices).

    `models` omitted: the eligible entries ranked strongest-tier-first then by name (a total order,
    so the result is stable), then de-duplicated by family, capped at `n`. With a single-tier
    `tiers`, every entry ranks equal, so the order is name-only — still a total order, still stable.
    """
    allowed = _normalize_tiers(tiers)
    if models is not None:
        by_name = {e["name"]: e for e in pool}
        chosen = [
            by_name[m]
            for m in models
            if m in by_name
            and _eligible(by_name[m], allowed)
            and ma.accepts_tools(by_name[m], tools)
        ]
        return _distinct_families(chosen, n)

    ranked = sorted(
        (e for e in pool if _eligible(e, allowed) and ma.accepts_tools(e, tools)),
        key=lambda e: (-ma.tier_rank(e["capacity_tier"]), e["name"]),
    )
    return _distinct_families(ma.remembered_first(ranked), n)


def _dispatch_one(
    entry: dict,
    prompt: str,
    timeout: int,
    tools: list[str] | None,
    env: dict[str, str],
) -> _SlotResult:
    """Call one model. `answer` set on success, `reason` set on failure — never both, never raises.

    Every failure mode (pi error, timeout, missing `pi`, a non-`cli` entry) becomes a reason string,
    so one dead entry in a parallel batch cannot take the batch down. An empty answer is a failure
    too. The entry's own `timeout`, when set, replaces the batch default.
    """
    timeout = ma.entry_timeout(entry, timeout)
    try:
        answer = ma.call_cli(entry, prompt, timeout, tools, env)
    except subprocess.CalledProcessError as exc:
        return _SlotResult(
            entry, None, f"pi exit {exc.returncode}: {ma.failure_detail(exc)}"
        )
    except subprocess.TimeoutExpired:
        return _SlotResult(entry, None, f"no answer within {timeout}s")
    except (OSError, ValueError) as exc:
        return _SlotResult(entry, None, str(exc))
    if not answer.strip():
        return _SlotResult(entry, None, "empty answer")
    return _SlotResult(entry, answer.strip(), None)


def run_panel(
    entries: list[dict],
    prompts: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    tools: list[str] | None = None,
) -> PanelResponse:
    """Send `prompts[i]` to `entries[i]` in parallel; collect answers in the entries' own order.

    `entries` and `prompts` must be the same length — pairing is positional, and a mismatch is a
    caller bug, not a runtime condition, so it raises. Each entry carries its own socket timeout, so
    a slow model is dropped as a skipped slot rather than stalling the batch — there is no retry.
    `entries` is expected pre-filtered by `select_for_panel`, which guarantees one entry per
    provider, so the fan-out never sends two concurrent calls to one provider and no per-provider
    `concurrency` cap is consulted. An `offline` entry slipping through is skipped, never called.
    `tools` goes to every slot's `pi` call — none by default.
    """
    if len(entries) != len(prompts):
        raise ValueError(
            f"run_panel: {len(entries)} entries but {len(prompts)} prompts — must match"
        )
    answers: list[Answer] = []
    skipped: list[Skip] = []
    if not entries:
        return PanelResponse(answers, skipped)
    env = ma.secret_env()

    def worker(entry: dict, prompt: str) -> _SlotResult:
        """Dispatch one (entry, prompt) pair, guarding the offline case that must not be called."""
        if entry.get("kind") == "offline":
            return _SlotResult(
                entry,
                None,
                "offline entry — route through the `offline` tool, not `panel`",
            )
        return _dispatch_one(entry, prompt, timeout, tools, env)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(entries)) as pool:
        for slot, res in enumerate(
            pool.map(call_scope.bind(worker), entries, prompts), start=1
        ):
            if res.reason is None and res.answer is not None:
                answers.append(
                    Answer(
                        slot, res.entry["name"], res.answer, ma.answer_label(res.entry)
                    )
                )
            else:
                skipped.append(Skip(slot, res.entry["name"], res.reason or "unknown"))
    return PanelResponse(answers, skipped)


def format_panel(response: PanelResponse, requested: int) -> str:
    """Render a panel run as text: a header, then one block per answer, each tagged with its slot.

    Every slot is named by its 1-based prompt index, so a caller that paired prompt *i* with its own
    role/lens *i* can map each answer straight back — even with gaps. The header separates the two
    ways a slot can be empty, because a caller filling the rest itself needs to tell them apart:
      - *skipped* — a selected model was tried and failed (no key / HTTP error / timeout), named.
      - *unfilled* — the pool had fewer eligible families than `requested`; the trailing slots were
        never assigned a model at all.
    """
    answered = len(response.answers)
    failed = len(response.skipped)
    unfilled_slots = list(range(answered + failed + 1, requested + 1))

    header = f"[panel] {answered}/{requested} slots answered"
    notes: list[str] = []
    if response.skipped:
        notes.append(
            "skipped: "
            + ", ".join(
                f"slot {s.slot} {s.model} ({s.reason})" for s in response.skipped
            )
        )
    if unfilled_slots:
        joined = ", ".join(str(n) for n in unfilled_slots)
        notes.append(
            f"unfilled: slot(s) {joined} — pool has fewer eligible families than requested"
        )
    if notes:
        header += " · " + " · ".join(notes)

    blocks = "\n\n".join(
        f"--- [slot {a.slot} · {a.label or a.model}] ---\n{a.text}"
        for a in response.answers
    )
    return f"{header}\n\n{blocks}" if blocks else header


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Dispatch one prompt per slot to a different pool family, in parallel."
    )
    ap.add_argument("--task", action="append", default=[], dest="tasks", required=True)
    ap.add_argument("--model", action="append", default=None, dest="models")
    ap.add_argument(
        "--tool",
        action="append",
        default=[],
        dest="tools",
        help="a pi tool every model may use (repeatable); none by default",
    )
    args = ap.parse_args(argv)

    entries = select_for_panel(
        ma.load_pool(), len(args.tasks), args.models, tools=args.tools or None
    )
    if not entries:
        print(
            "[panel] BLOCKED — no eligible model in the pool (need kind=cli, tier sonnet/opus).",
            file=sys.stderr,
        )
        return 2

    prompts = args.tasks[: len(entries)]
    start = time.time()
    response = run_panel(entries, prompts, tools=args.tools or None)
    if not response.answers:
        print(
            f"[panel] BLOCKED — every candidate failed. Skipped: {response.skipped}",
            file=sys.stderr,
        )
        return 2

    print(
        f"[panel] {len(response.answers)}/{len(args.tasks)} answered · {time.time() - start:.0f}s",
        file=sys.stderr,
    )
    print(format_panel(response, len(args.tasks)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
