#!/usr/bin/env python3
"""Per pool entry, the `capacity_tier` its answers support: three diffs with one planted bug each.

Each entry is sent three fixtures, twice each, through the pool's own call path (`call_cli` with
`read` and `grep`): a diff in a temp folder, next to the files it changes. The fixtures are one per
level: `haiku` an obvious one-line bug, `sonnet` a logic error spread over two files, `opus` a
subtle security bug. A reply finds the bug when it names the expected file and line — checkable
without a model; extra remarks do not matter, they are counted and reported next to the tier.

The tier is the highest level whose bug is found in both replies. An entry that does not find the
`haiku` bug twice is `sous haiku`, whatever it finds above. A call that fails counts as a miss and
as an error.

Only `kind: "cli"` entries with `tools: true` are probed: an entry without a tool loop cannot read
the files. The tier is a proposal printed as a table — nothing is written, not to the pool
configuration, not anywhere: the pool belongs to whoever edits it.

Usage:
    model_tier_probe.py [--name ENTRY ...]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).parent))
import call_scope  # noqa: E402
import model_adversarial as ma  # noqa: E402

ASKS = 2
STEP_BUDGET = 6
SOUS_HAIKU = "sous haiku"
DIFF_FILE = "change.diff"

# `file:line`, `file line N` or `file, line N`, the file possibly quoted or in backticks.
_FINDING_RE = re.compile(r"([\w./-]+\.\w+)[`*\"']*(?::|,?\s+lines?\s+)(\d+)(?!\d)")


class Fixture(NamedTuple):
    """One probe level: a diff, the files after the change, and where its single bug is."""

    level: str
    sources: dict[str, str]
    diff: str
    file: str
    line: int
    marker: str


FIXTURES = (
    Fixture(
        level="haiku",
        sources={
            "average.py": (
                "def average(values):\n"
                '    """Mean of a non-empty list."""\n'
                "    total = 0\n"
                "    for v in values:\n"
                "        total += v\n"
                "    return total / (len(values) + 1)\n"
            )
        },
        diff=(
            "--- a/average.py\n"
            "+++ b/average.py\n"
            "@@ -1,6 +1,6 @@\n"
            " def average(values):\n"
            '     """Mean of a non-empty list."""\n'
            "     total = 0\n"
            "     for v in values:\n"
            "         total += v\n"
            "-    return total / len(values)\n"
            "+    return total / (len(values) + 1)\n"
        ),
        file="average.py",
        line=6,
        marker="len(values) + 1",
    ),
    Fixture(
        level="sonnet",
        sources={
            "pricing.py": (
                "BULK_THRESHOLD = 10\n"
                "\n"
                "\n"
                "def discount_rate(quantity):\n"
                '    """Discount in percent (0 to 15) for a quantity."""\n'
                "    if quantity >= BULK_THRESHOLD:\n"
                "        return 15\n"
                "    return 0\n"
            ),
            "cart.py": (
                "from pricing import discount_rate\n"
                "\n"
                "\n"
                "def line_total(unit_price, quantity):\n"
                '    """Total of one cart line, discount applied."""\n'
                "    gross = unit_price * quantity\n"
                "    return gross * (1 - discount_rate(quantity))\n"
            ),
        },
        diff=(
            "--- a/pricing.py\n"
            "+++ b/pricing.py\n"
            "@@ -4,5 +4,5 @@\n"
            " def discount_rate(quantity):\n"
            '-    """Discount rate (0 to 0.15) for a quantity."""\n'
            '+    """Discount in percent (0 to 15) for a quantity."""\n'
            "     if quantity >= BULK_THRESHOLD:\n"
            "-        return 0.15\n"
            "+        return 15\n"
            "     return 0\n"
        ),
        file="cart.py",
        line=7,
        marker="1 - discount_rate(quantity)",
    ),
    Fixture(
        level="opus",
        sources={
            "storage.py": (
                "import os\n"
                "\n"
                'BASE_DIR = "/srv/files"\n'
                "\n"
                "\n"
                "def resolve(name):\n"
                '    """Path of `name` inside BASE_DIR; refuse anything that leaves it."""\n'
                "    path = os.path.normpath(os.path.join(BASE_DIR, name))\n"
                "    if not path.startswith(BASE_DIR):\n"
                '        raise ValueError("outside the storage directory")\n'
                "    return path\n"
            )
        },
        diff=(
            "--- a/storage.py\n"
            "+++ b/storage.py\n"
            "@@ -6,6 +6,6 @@\n"
            " def resolve(name):\n"
            '     """Path of `name` inside BASE_DIR; refuse anything that leaves it."""\n'
            "     path = os.path.normpath(os.path.join(BASE_DIR, name))\n"
            "-    if os.path.commonpath([path, BASE_DIR]) != BASE_DIR:\n"
            "+    if not path.startswith(BASE_DIR):\n"
            '         raise ValueError("outside the storage directory")\n'
            "     return path\n"
        ),
        file="storage.py",
        line=9,
        marker="path.startswith(BASE_DIR)",
    ),
)


class LevelResult(NamedTuple):
    """One level's outcome: replies naming the bug, and the off-target remarks of each reply."""

    level: str
    hits: int
    off_target: list[int]


class Skipped(NamedTuple):
    """An entry left out of the probe, and why."""

    name: str
    reason: str


class EntryTier(NamedTuple):
    """One table row: the proposed tier and what it rests on."""

    name: str
    tier: str
    levels: list[LevelResult]
    errors: int = 0

    @property
    def remarks(self) -> int:
        """Off-target remarks over every reply."""
        return sum(sum(lv.off_target) for lv in self.levels)


class ProbeRun(NamedTuple):
    """What a probe run returns: the probed entries in pool order, and those left out."""

    results: list[EntryTier]
    skipped: list[Skipped]


def _findings(reply: str) -> set[tuple[str, int]]:
    """The `(file name, line)` pairs a reply points at."""
    return {(Path(f).name, int(n)) for f, n in _FINDING_RE.findall(reply)}


def names_the_bug(fixture: Fixture, reply: str) -> bool:
    """Whether `reply` names the fixture's expected file and line."""
    return (fixture.file, fixture.line) in _findings(reply)


def off_target_remarks(fixture: Fixture, reply: str) -> int:
    """How many findings of `reply` point somewhere else than the planted bug."""
    return len(_findings(reply) - {(fixture.file, fixture.line)})


def _prompt(directory: Path, fixture: Fixture) -> str:
    """Write the fixture into `directory` and return the prompt that sends the model to it."""
    (directory / DIFF_FILE).write_text(fixture.diff, encoding="utf-8")
    for name, content in fixture.sources.items():
        (directory / name).write_text(content, encoding="utf-8")
    names = ", ".join([DIFF_FILE, *fixture.sources])
    return (
        f"Use your tools to review a change. The folder {directory} holds its diff and the "
        f"files after the change: {names}. The change contains exactly one bug. Reply with "
        "your findings only, one per line, as `file:line: description`, where file is the "
        "file's name and line its line number in the file after the change."
    )


def _asked(
    entry: dict[str, Any], fixture: Fixture, timeout: int, env: dict[str, str]
) -> str | None:
    """One reply of `entry` to `fixture`; None when the call failed."""
    with tempfile.TemporaryDirectory(prefix="pinky-tier-probe-") as tmp:
        prompt = _prompt(Path(tmp), fixture)
        try:
            return ma.call_cli(entry, prompt, timeout, tools=ma.PROBE_TOOLS, env=env)
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
            ValueError,
        ):
            return None


def probe_entry(entry: dict[str, Any], env: dict[str, str]) -> EntryTier:
    """Ask `entry` every fixture `ASKS` times and propose its tier.

    Args:
        entry: a `kind: "cli"` pool entry; its own `timeout` wins over the 20 s per step budget.
        env: the provider keys for `pi`.

    Returns:
        The tier, the per-level hits and off-target remarks, and the number of failed calls.
    """
    timeout = ma.entry_timeout(entry, STEP_BUDGET * ma.PROBE_STEP_SECONDS)
    levels: list[LevelResult] = []
    errors = 0
    for fixture in FIXTURES:
        replies = [_asked(entry, fixture, timeout, env) for _ in range(ASKS)]
        errors += replies.count(None)
        texts = [r or "" for r in replies]
        levels.append(
            LevelResult(
                fixture.level,
                sum(names_the_bug(fixture, t) for t in texts),
                [off_target_remarks(fixture, t) for t in texts],
            )
        )
    found = [lv.level for lv in levels if lv.hits == ASKS]
    tier = found[-1] if found and found[0] == "haiku" else SOUS_HAIKU
    return EntryTier(entry["name"], tier, levels, errors)


def skip_reason(entry: dict[str, Any]) -> str | None:
    """Why `entry` is not probed; None when it is."""
    if entry.get("kind") != "cli":
        return "not kind: cli"
    if entry.get("tools") is False:
        return "tools: false"
    if entry.get("tools") is not True:
        return "tools: not true"
    return None


def probe_pool(
    pool: list[dict[str, Any]], env: dict[str, str] | None = None
) -> ProbeRun:
    """Probe every eligible entry: one provider's entries one after another, providers in parallel.

    Args:
        pool: the entries to consider.
        env: the provider keys; None reads them with `secret_env`.

    Returns:
        The probed entries in pool order, and each entry left out with its reason.
    """
    env = ma.secret_env() if env is None else env
    skipped = [Skipped(e["name"], r) for e in pool if (r := skip_reason(e))]
    eligible = [e for e in pool if skip_reason(e) is None]
    groups: dict[Any, list[int]] = {}
    for i, entry in enumerate(eligible):
        groups.setdefault(entry.get("provider") or ("", i), []).append(i)
    results: list[EntryTier | None] = [None] * len(eligible)

    def run_group(indexes: list[int]) -> None:
        for i in indexes:
            results[i] = probe_entry(eligible[i], env)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(groups))) as ex:
        list(ex.map(call_scope.bind(run_group), groups.values()))
    return ProbeRun([r for r in results if r is not None], skipped)


def render(results: list[EntryTier], skipped: list[Skipped]) -> str:
    """The table: tier, per-level hits out of `ASKS`, off-target remarks and failed calls."""
    name_w = max([len("entry"), *(len(r.name) for r in results)])
    tier_w = max([len("tier"), *(len(r.tier) for r in results)])
    lines = [
        f"{'entry'.ljust(name_w)}  {'tier'.ljust(tier_w)}  haiku  sonnet  opus  "
        "remarks  errors"
    ]
    for r in results:
        hits = [f"{lv.hits}/{ASKS}" for lv in r.levels]
        lines.append(
            f"{r.name.ljust(name_w)}  {r.tier.ljust(tier_w)}  {hits[0].ljust(5)}  "
            f"{hits[1].ljust(6)}  {hits[2].ljust(4)}  {r.remarks:>7}  {r.errors:>6}"
        )
    lines += [
        "",
        "Proposed only — nothing is written; edit `capacity_tier` in the pool yourself.",
    ]
    if skipped:
        lines.append("skipped: " + ", ".join(f"{n} ({why})" for n, why in skipped))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Print the proposed tier per pool entry; write nothing."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--name",
        action="append",
        default=[],
        dest="names",
        help="only this pool entry (repeatable); every entry by default",
    )
    args = ap.parse_args(argv)
    pool = ma.filter_pool(ma.load_pool(), None, args.names)
    if not pool:
        print(ma.filter_miss(None, args.names), file=sys.stderr)
        return 2
    print(render(*probe_pool(pool)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
