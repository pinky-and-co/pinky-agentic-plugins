#!/usr/bin/env python3
"""Stop/SessionEnd hook — auto-maintain the in-progress card's handoff (kanban feature).

Fires on `Stop` (end of an assistant turn) and `SessionEnd` (a `Stop` hook does not fire on a
user-interrupted turn — `SessionEnd` is the safety net that still refreshes the breadcrumb once,
see `hooks-guide.md`). Non-blocking either way: it never sends the agent back to work. Its job is
mechanical — keep a managed breadcrumb fresh inside the `_KANBAN/CHANTIER*/3-in-progress/*.md` cards
WHOSE SCOPE MATCHES the session's repo (a card opts in via its `repos:` frontmatter — or the legacy
`**Repos:**` line during the board's ongoing YAML conversion, see `card_repos` — or its existing
block's repo) so the next session has the current state without depending on anyone to trigger it.
The narrative handoff stays the agent's job (skill `kanban`); this only maintains the auto block
between the markers.

Design:
- runs only when there is real work to record (a git working tree with changes) — never churns on idle.
- records branch · uncommitted files · recent commit subjects · a develop→main landmark.
- carries the anti-secrets directive: commit subjects are scrubbed before being written.

This deliberately omits a signal comparing the card's declared `model:` to the model that actually
ran the turn: the event itself carries no model name. The transcript at the event's
`transcript_path` does (per assistant message), but on `Stop` it is written asynchronously and may
lag the current turn — not wired in rather than read from a possibly stale file.

Python 3.11+, stdlib only. Exit code is always 0 (a hook failure must never block a Stop/SessionEnd).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent))
from pc_kanban_secret_patterns import scrub  # noqa: E402

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

BLOCK_START = (
    "<!-- auto-handoff:start (maintained by stop_handoff.py — do not edit by hand) -->"
)
BLOCK_END = "<!-- auto-handoff:end -->"
ANTI_SECRETS = "never include secrets / credentials / internal URLs in a handoff"  # pragma: allowlist secret
MAX_FILES = 20
MAX_AGENT_LOGS = 12
MAX_UNREVIEWED_DONE = 10
STALE_THRESHOLDS_DAYS = {"0-ideas": 60, "1-todo": 30}
EXPORTED_STALE_THRESHOLD_DAYS = 14
MAX_STALE = 10

DEFAULT_PRIORITY = 3


def card_priority(fm: dict[str, object]) -> int:
    """A card's `priority:` (1-5), clamped to that range, or `DEFAULT_PRIORITY` when absent or
    unparseable — never raises."""
    raw = fm.get("priority")
    if isinstance(raw, str):
        try:
            return max(1, min(5, int(raw)))
        except ValueError:
            pass
    return DEFAULT_PRIORITY


def stale_threshold(base_days: int, priority: int) -> float:
    """`--list-stale` threshold, scaled by priority: `base × 2^((3-priority)/2)`, `base` being
    `STALE_THRESHOLDS_DAYS` at the default priority."""
    return base_days * 2 ** ((DEFAULT_PRIORITY - priority) / 2)


MAX_STATUS_CHARS = 200


def read_event() -> dict:
    """Parse the hook JSON from stdin; tolerate an empty / malformed payload."""
    try:
        return json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return {}


# ── Declared boards — read from the user-level `kanban_boards` key ────────────
SETTINGS_PINKY_PATH = Path.home() / ".claude" / "settings.pinky.json"


def first_heading(path: Path) -> str:
    """First `# ` line of a markdown file — a journal entry's title, for the listing."""
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return ""


def _kanban_boards() -> dict[str, list[str]]:
    """The `kanban_boards` dict of `SETTINGS_PINKY_PATH`, or `{}` when absent/malformed/wrong-shaped."""
    if not SETTINGS_PINKY_PATH.is_file():
        return {}
    try:
        data = json.loads(SETTINGS_PINKY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    boards = data.get("kanban_boards") if isinstance(data, dict) else None
    return boards if isinstance(boards, dict) else {}


def declared_kanbans(primary_root: Path) -> list[Path]:
    """Boards declared for `primary_root` — `kanban_boards[<primary_root, resolved>]` of
    `SETTINGS_PINKY_PATH`, the sole source (written by skill `init`). `primary_root` is the parent
    of the nearest `_KANBAN/` already found by `find_kanbans`, never an arbitrary path — a
    board declares its extra roots under its own key, not under a subdirectory of it."""
    roots = _kanban_boards().get(str(primary_root.resolve()))
    if not isinstance(roots, list):
        return []
    return [Path(r) / "_KANBAN" for r in roots if isinstance(r, str)]


def find_kanbans(start: Path) -> list[Path]:
    """Every `_KANBAN/` this session may write to: the nearest one, plus any board declared for
    it in `kanban_boards`. No nearest board (no `_KANBAN/` ancestor from `start`/
    `CLAUDE_PROJECT_DIR`) means no primary key to look up — a session with nothing local never
    sees a declared board either."""
    found: list[Path] = []
    candidates = [start, *start.parents]
    proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj:
        p = Path(proj)
        candidates = [p, *p.parents, *candidates]
    primary_root: Path | None = None
    for base in candidates:
        kanban = base / "_KANBAN"
        if kanban.is_dir():
            found.append(kanban)
            primary_root = base
            break
    if primary_root is not None:
        for kanban in declared_kanbans(primary_root):
            if kanban.is_dir() and kanban not in found:
                found.append(kanban)
    return found


def agent_log_dir(kanban: Path) -> Path:
    """The cross-agent journal directory for a board — nested under its `_KANBAN/` root once
    migrated (`pc_kanban_migrate_agent_log.py`), the pre-migration sibling until then. Falls back
    to the sibling only when the nested dir does not exist and the sibling does — a board never
    migrated (or mounted read-only from a machine that hasn't run the script yet) still resolves
    to its real content instead of a directory nobody has created."""
    nested = kanban / "_AGENT_LOG"
    sibling = kanban.parent / "_AGENT_LOG"
    if not nested.is_dir() and sibling.is_dir():
        return sibling
    return nested


def inprogress_cards(kanban: Path) -> list[Path]:
    """Every card currently in a chantier's `3-in-progress/` column."""
    return sorted(kanban.glob("CHANTIER*/3-in-progress/*.md"))


# Lenient on spacing, checkbox case and the `(`/`:` delimiter — the convention writes
# `- [x] reviewed (…)` at column 0, but a hand-edited card drifts and the audit must not miss it.
_REVIEWED_DONE_RE = re.compile(r"^\s*-\s+\[[xX]\]\s+reviewed\s*[(:]", re.MULTILINE)
_REVIEWED_OPEN_RE = re.compile(r"^\s*-\s+\[ \]\s+reviewed\s*[(:]", re.MULTILINE)
_REVIEWED_MECHANICAL_RE = re.compile(
    r"^\s*-\s+\[[xX]\]\s+reviewed\s*\(\s*n/a", re.MULTILINE
)


class UnreviewedDone(NamedTuple):
    """Result of `unreviewed_done_cards` — see that function's docstring."""

    flagged: list[tuple[Path, str]]
    mechanical: int


def unreviewed_done_cards(kanban: Path) -> UnreviewedDone:
    """`4-done/` cards missing a checked `reviewed (...)` line, plus the mechanical count.

    Args:
        kanban: a board root, as returned by `find_kanbans`.

    Returns:
        An `UnreviewedDone`. `.flagged` holds `(card_path, tag)` pairs ordered by the
        card's `date:` frontmatter descending — recent work first; `date:` is set at
        intake rather than close but stays stable where a sync drive rewrites mtime — a
        missing `date:` sorting last, then by filename. `tag` is `reviewed unchecked`
        when an unchecked `- [ ] reviewed (...)` line is present (review declared needed,
        then skipped), or `no review line` when no such line exists. `.mechanical`
        counts cards closed `- [x] reviewed (n/a — mechanical)`: a real method line keeps a
        card out of both buckets, this escape hatch stays a visible number so its rate
        cannot drift the way the un-mechanized convention did.
    """
    flagged: list[tuple[str, Path, str]] = []
    mechanical = 0
    for card in kanban.glob("CHANTIER*/4-done/*.md"):
        try:
            text = card.read_text(encoding="utf-8")
        except OSError:
            continue
        if _REVIEWED_MECHANICAL_RE.search(text):
            mechanical += 1
            continue
        if _REVIEWED_DONE_RE.search(text):
            continue
        tag = (
            "reviewed unchecked" if _REVIEWED_OPEN_RE.search(text) else "no review line"
        )
        fm = parse_frontmatter(text) or {}
        date = fm.get("date")
        flagged.append((date if isinstance(date, str) else "", card, tag))
    flagged.sort(key=lambda row: (row[0], row[1].name), reverse=True)
    return UnreviewedDone([(card, tag) for _, card, tag in flagged], mechanical)


class StaleCard(NamedTuple):
    """One row of `stale_cards`'s result: the card, its column, and how many days since its
    `date:` frontmatter last changed."""

    card: Path
    column: str
    days_idle: int


def stale_cards(kanban: Path, today: date | None = None) -> list[StaleCard]:
    """`0-ideas`/`1-todo` cards whose `date:` frontmatter is older than `stale_threshold(
    STALE_THRESHOLDS_DAYS[column], card_priority(...))` — a fallback net behind the closing-time
    proposal (`finishing-a-card.md` § "Proposing the next card"), for a card no closure ever
    crosses. `2-selected-for-dev` is deliberately absent from `STALE_THRESHOLDS_DAYS`, so it can
    never appear here however old (its immobility is a priority problem, not obsolescence).

    A `3-in-progress` card carrying an `exported:` marker (the `pc-delegate` "aller" leg's signal
    that it left for an agent working outside Claude Code) is checked the same way, against
    `EXPORTED_STALE_THRESHOLD_DAYS` — this is the only way `3-in-progress` ever appears here; a
    card taken into progress locally never carries that marker. A missing or unparseable `date:`
    is skipped, never guessed.

    Args:
        kanban: a board root, as returned by `find_kanbans`.
        today: injectable for tests; defaults to the real current date.

    Returns:
        Rows sorted by `days_idle` descending — most idle first.
    """
    today = today or date.today()
    flagged: list[StaleCard] = []
    for column, base_days in STALE_THRESHOLDS_DAYS.items():
        for card in kanban.glob(f"CHANTIER*/{column}/*.md"):
            try:
                text = card.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = parse_frontmatter(text) or {}
            raw_date = fm.get("date")
            if not isinstance(raw_date, str) or not raw_date:
                continue
            try:
                card_date = date.fromisoformat(raw_date)
            except ValueError:
                continue
            days_idle = (today - card_date).days
            if days_idle > stale_threshold(base_days, card_priority(fm)):
                flagged.append(StaleCard(card, column, days_idle))
    for card in kanban.glob("CHANTIER*/3-in-progress/*.md"):
        try:
            text = card.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = parse_frontmatter(text) or {}
        if not fm.get("exported"):
            continue
        raw_date = fm.get("date")
        if not isinstance(raw_date, str) or not raw_date:
            continue
        try:
            card_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        days_idle = (today - card_date).days
        if days_idle > stale_threshold(
            EXPORTED_STALE_THRESHOLD_DAYS, card_priority(fm)
        ):
            flagged.append(StaleCard(card, "3-in-progress", days_idle))
    flagged.sort(key=lambda row: row.days_idle, reverse=True)
    return flagged


_KANBAN_COLUMNS = ("0-ideas", "1-todo", "2-selected-for-dev", "3-in-progress", "4-done")


def _board_table_lines(index_text: str) -> list[str]:
    """The `## Board` markdown table only — header, separator, rows.

    `INDEX.md` carries onboarding prose above the table (preamble, topology, the column
    convention) that the `kanban` skill's own docs already own — `--list-chantiers`'s default
    mode drops it, keeping only what a session needs to orient.
    """
    lines = index_text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith("| #")), None
    )
    if start is None:
        return []
    table: list[str] = []
    for line in lines[start:]:
        if not line.lstrip().startswith("|"):
            break
        table.append(line)
    return table


_UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")


def _split_table_row(line: str) -> list[str]:
    """Split a markdown table row on `|`, treating a `\\|` (GFM's own escape for a literal pipe
    inside a cell) as cell content rather than a column delimiter — a naive `line.split("|")`
    shifts every cell after the first escaped pipe, so a cap or an id lookup targets the wrong
    field. Splitting only on unescaped pipes and rejoining the result with `"|"` reproduces the
    original line exactly, since no split point ever falls inside an escaped pipe.
    """
    return _UNESCAPED_PIPE_RE.split(line)


def _cap_row_status(line: str) -> str:
    """Cap a data row's Statut cell (the last `|`-delimited one) to `MAX_STATUS_CHARS`.

    A data row is identified by a 4-digit chantier id in its first cell — the header and
    separator rows have no Statut prose to cap and pass through unchanged.
    """
    cells = _split_table_row(line)
    if len(cells) < 4 or not cells[1].strip().isdigit():
        return line
    stripped = cells[-2].strip()
    if len(stripped) <= MAX_STATUS_CHARS:
        return line
    omitted = len(stripped) - MAX_STATUS_CHARS
    cells[-2] = f" {stripped[:MAX_STATUS_CHARS]}… (+{omitted} car.) "
    return "|".join(cells)


CARDS_LEGEND = "cards legend: " + "/".join(_KANBAN_COLUMNS)


def _column_counts(chantier_dir: Path | None) -> str:
    """`N/N/N/N/N` in `_KANBAN_COLUMNS` order (see `CARDS_LEGEND`), walked from the filesystem.

    Positional rather than `col=N` per column — one legend line for the whole invocation
    replaces a repeated label on every chantier row.
    """
    return "/".join(
        str(sum(1 for _ in (chantier_dir / col).glob("*.md")) if chantier_dir else 0)
        for col in _KANBAN_COLUMNS
    )


def bounded_board_lines(kanban: Path) -> list[str]:
    """The bounded `--list-chantiers` default: table rows only, Statut capped, cards/column added.

    Args:
        kanban: a board root, as returned by `find_kanbans`.

    Returns:
        One line per table row (header/separator included, unchanged) plus, right after each
        data row, a `  cards: N/N/N/N/N` line with that chantier's per-column card count — see
        `CARDS_LEGEND` for the column order. `[]` when `INDEX.md` is missing or has
        no `## Board` table.
    """
    index = kanban / "INDEX.md"
    if not index.is_file():
        return []
    table = _board_table_lines(index.read_text(encoding="utf-8"))
    ids_to_dirs = {
        m.group(1): d
        for d in kanban.glob("CHANTIER*")
        if d.is_dir() and (m := re.match(r"CHANTIER(\d{4})-", d.name))
    }
    out: list[str] = []
    for line in table:
        cells = _split_table_row(line)
        chantier_id = cells[1].strip() if len(cells) >= 4 else ""
        out.append(_cap_row_status(line))
        if chantier_id.isdigit():
            out.append(f"  cards: {_column_counts(ids_to_dirs.get(chantier_id))}")
    return out


def _git(cwd: Path, *args: str) -> str | None:
    """Run a git command in `cwd`; return stripped stdout, or None on any failure."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def parse_changed(status: str) -> list[str]:
    """Extract the changed paths from `git status --porcelain` output."""
    out: list[str] = []
    for line in status.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            out.append(parts[1])
    return out


_REMOTE_SLUG_RE = re.compile(
    r"^(?:git@|https://|http://)[^:/]+[:/](?P<slug>.+?)(?:\.git)?$"
)


def remote_slug(cwd: Path) -> str | None:
    """`org/repo` from `git remote get-url origin`, host-agnostic (SSH or HTTPS, any forge).

    A directory basename collides across orgs (two repos can share a name); the remote does not —
    same identity kanban cards use for `repos:` (skill `kanban`). None when there is no `origin`
    remote (a local-only repo) — the caller falls back to the directory name.
    """
    url = _git(cwd, "remote", "get-url", "origin")
    if not url:
        return None
    m = _REMOTE_SLUG_RE.match(url)
    return m.group("slug") if m else None


def git_context(cwd: Path) -> dict | None:
    """Mechanical state of the repo at `cwd`, or None when there is nothing worth recording."""
    if _git(cwd, "rev-parse", "--is-inside-work-tree") != "true":
        return None
    status = _git(cwd, "status", "--porcelain")
    if not status:  # clean tree → nothing to record
        return None
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "?"
    changed = parse_changed(status)
    log = _git(cwd, "log", "-3", "--format=%s") or ""
    recent = [scrub(s) for s in log.splitlines() if s]
    slug = remote_slug(cwd)
    return {
        "repo": slug or cwd.name,  # display value for the block — prefer the slug
        "repo_name": cwd.name,
        "repo_slug": slug,
        "branch": branch,
        "changed": changed,
        "recent": recent,
        "landmark": branch in {"main", "master"},
    }


# ── Card scope resolution — dual format during the board's YAML conversion ──
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
CARD_REPOS_RE = re.compile(r"^\*\*Repos?\s*:\*\*\s*(.+)$", re.MULTILINE)
_BLOCK_REPO_RE = re.compile(
    re.escape(BLOCK_START) + r".*?Repo / branch:\s*`([^`]+)`", re.DOTALL
)


def parse_frontmatter(text: str) -> dict[str, object] | None:
    """Minimal YAML frontmatter reader for the card schema (skill `kanban`) — not a general YAML
    parser. Handles flat `key: value` and `key: [a, b]` / `key: []` only, the two shapes the
    schema uses. No nesting, no multi-line scalars — stdlib only, by design (this hook is
    distributed standalone). Returns None when the text carries no `---`-delimited frontmatter."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    data: dict[str, object] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            data[key] = [v.strip().strip("\"'") for v in inner.split(",") if v.strip()]
        elif value in ("null", "~", ""):
            data[key] = None
        else:
            data[key] = value.strip("\"'")
    return data


def card_repos(card_text: str) -> list[str] | None:
    """Declared repo-scope for a card, as-is: YAML frontmatter `repos:` (org/repo slugs, skill
    `kanban`) first, then the legacy `**Repos:**` line still found on cards not yet converted
    (bare directory names). None means neither is declared. Orientation only — matching a specific
    session's repo needs both identities, see card_accepts."""
    fm = parse_frontmatter(card_text)
    if fm is not None and "repos" in fm:
        repos = fm["repos"]
        return list(repos) if isinstance(repos, list) else [str(repos)]
    declared = CARD_REPOS_RE.search(card_text)
    if declared:
        names = {t.strip("`").strip() for t in re.split(r"[,\s]+", declared.group(1))}
        return sorted(names - {""})
    return None


def card_accepts(card_text: str, repo_name: str, repo_slug: str | None) -> bool:
    """Whether this session's breadcrumb belongs on this card — the anti-cross-contamination filter.

    Without this filter, a single Stop would stamp the SAME block (the cwd repo's git state) into
    every in-progress card of every chantier, clobbering unrelated breadcrumbs. Two repo identities
    are checked
    because the board carries both card formats during its ongoing YAML conversion: a YAML
    `repos:` declares `org/repo` slugs (matched against `repo_slug`, a bare name collides across
    orgs) ; the legacy `**Repos:**` line declares bare directory names (matched against
    `repo_name`). Precedence: a declared repo-scope wins in its own format (it also heals a block
    inherited from a past contamination) ; else the existing block's own repo decides, checked
    against either identity ; a card with neither never gets a blind block — a wrong breadcrumb is
    worse than none.
    """
    fm = parse_frontmatter(card_text)
    if fm is not None and "repos" in fm:
        repos = fm["repos"]
        repos = list(repos) if isinstance(repos, list) else [str(repos)]
        return repo_slug is not None and repo_slug in repos
    declared = CARD_REPOS_RE.search(card_text)
    if declared:
        names = {t.strip("`").strip() for t in re.split(r"[,\s]+", declared.group(1))}
        return repo_name in (names - {""})
    block = _BLOCK_REPO_RE.search(card_text)
    if block:
        return block.group(1) in {repo_name, repo_slug}
    return False


def build_block(ctx: dict) -> str:
    """Render the managed auto-handoff block from the mechanical git context.

    Deliberately carries NO timestamp: the block must be a pure function of git state so it only
    rewrites when the state actually changes. Git and the narrative handoff own the "when".
    """
    files = ctx["changed"]
    shown = ", ".join(files[:MAX_FILES])
    if len(files) > MAX_FILES:
        shown += f", … (+{len(files) - MAX_FILES})"
    lines = [
        BLOCK_START,
        "## Handoff (auto)",
        f"- Repo / branch: `{ctx['repo']}` @ `{ctx['branch']}`"
        + ("  ⚑ develop→main landmark" if ctx["landmark"] else ""),
        f"- Uncommitted ({len(files)}): {shown or '—'}",
        "- Recent commits: " + (" · ".join(ctx["recent"]) if ctx["recent"] else "—"),
        f"- _Auto block — {ANTI_SECRETS}. Narrative handoff above is the agent's job._",
        BLOCK_END,
    ]
    return "\n".join(lines)


def upsert_block(card_text: str, block: str) -> str:
    """Replace the managed block in `card_text`, or append it when absent. Idempotent."""
    pattern = re.compile(
        re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END), re.DOTALL
    )
    if pattern.search(card_text):
        return pattern.sub(lambda _: block, card_text)
    sep = "" if card_text.endswith("\n") else "\n"
    return f"{card_text}{sep}\n{block}\n"


def refresh_cards(cards: list[Path], ctx: dict) -> None:
    """Upsert the auto block into the cards whose scope matches ctx's repo (see card_accepts)."""
    block = build_block(ctx)
    for card in cards:
        try:
            original = card.read_text(encoding="utf-8")
        except OSError:
            continue
        if not card_accepts(original, ctx["repo_name"], ctx["repo_slug"]):
            continue
        updated = upsert_block(original, block)
        if updated != original:  # only write on real change — no mtime churn
            try:
                card.write_text(updated, encoding="utf-8")
            except OSError:
                continue


def main() -> int:
    if "--list-kanbans" in sys.argv[1:]:
        for kanban in find_kanbans(Path(os.getcwd())):
            print(kanban)
        return 0

    if "--list-agent-log-dirs" in sys.argv[1:]:
        for kanban in find_kanbans(Path(os.getcwd())):
            print(agent_log_dir(kanban))
        return 0

    if sys.argv[1:2] == ["--card-path"]:
        cid, slug = sys.argv[2], sys.argv[3]
        hits = [
            c
            for kanban in find_kanbans(Path(os.getcwd()))
            for c in kanban.glob(f"CHANTIER{cid}-*/[0-4]-*/{slug}.md")
        ]
        if len(hits) != 1:
            print(
                f"stop_handoff: '{slug}' in chantier {cid} matched {len(hits)} cards",
                file=sys.stderr,
            )
            return 1
        print(hits[0])
        return 0

    if sys.argv[1:2] == ["--card-repos"]:
        text = Path(sys.argv[2]).read_text(encoding="utf-8")
        for slug in card_repos(text) or []:
            print(slug)
        return 0

    if sys.argv[1:2] == ["--card-model"]:
        fm = parse_frontmatter(Path(sys.argv[2]).read_text(encoding="utf-8")) or {}
        model = fm.get("model")
        if isinstance(model, str) and model:
            print(model)
        return 0

    if sys.argv[1:2] == ["--card-effort"]:
        fm = parse_frontmatter(Path(sys.argv[2]).read_text(encoding="utf-8")) or {}
        effort = fm.get("effort")
        if isinstance(effort, str):
            effort = effort.split("#", 1)[0].strip()
            if effort:
                print(effort)
        return 0

    if "--list-chantiers" in sys.argv[1:]:
        full = "--full" in sys.argv[1:]
        if not full:
            print(CARDS_LEGEND)
        for kanban in find_kanbans(Path(os.getcwd())):
            print(f"=== BOARD: {kanban}")
            index = kanban / "INDEX.md"
            if index.is_file():
                if full:
                    print(index.read_text(encoding="utf-8").rstrip())
                else:
                    for line in bounded_board_lines(kanban):
                        print(line)
            print("--- chantiers:")
            for d in sorted(kanban.glob("CHANTIER*")):
                if d.is_dir():
                    print(d.name)
            print()
        return 0

    if "--list-unreviewed-done" in sys.argv[1:]:
        for kanban in find_kanbans(Path(os.getcwd())):
            flagged, mechanical = unreviewed_done_cards(kanban)
            if not flagged and not mechanical:
                continue
            print(f"=== BOARD: {kanban}")
            for card, tag in flagged[:MAX_UNREVIEWED_DONE]:
                print(f"{card.relative_to(kanban)}  ({tag})")
            extra = len(flagged) - MAX_UNREVIEWED_DONE
            if extra > 0:
                print(f"… (+{extra} more)")
            if mechanical:
                print(f"— {mechanical} closed `reviewed (n/a — mechanical)`")
            print()
        return 0

    if "--list-stale" in sys.argv[1:]:
        for kanban in find_kanbans(Path(os.getcwd())):
            stale = stale_cards(kanban)
            if not stale:
                continue
            print(f"=== BOARD: {kanban}")
            for card, column, days_idle in stale[:MAX_STALE]:
                print(f"{card.relative_to(kanban)}  ({column}, {days_idle}j)")
            extra = len(stale) - MAX_STALE
            if extra > 0:
                print(f"… (+{extra} more)")
            print()
        return 0

    if "--list-agent-logs" in sys.argv[1:]:
        for kanban in find_kanbans(Path(os.getcwd())):
            log_dir = agent_log_dir(kanban)
            if not log_dir.is_dir():
                continue
            recent = sorted(
                log_dir.glob("*.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:MAX_AGENT_LOGS]
            if not recent:
                continue
            print(f"=== ORG: {kanban.parent}")
            for entry in recent:
                print(f"{entry.name} — {first_heading(entry)}")
            print()
        return 0

    event = read_event()
    cwd = Path(event.get("cwd") or os.getcwd())
    kanbans = find_kanbans(cwd)
    if not kanbans:
        return 0
    cards = [c for k in kanbans for c in inprogress_cards(k)]
    if not cards:
        return 0
    ctx = git_context(cwd)
    if ctx is None:
        return 0
    refresh_cards(cards, ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
