---
name: kanban
description: Mechanics of the file-based kanban board used to track chantiers (epics) and cards (tasks) — column lifecycle, card frontmatter schema, filiation, cross-agent log. Use when reading, moving, or writing a `_KANBAN/CHANTIER*/**/*.md` card, or when a session needs to resolve which repos/model/goal a card implies.
user-invocable: false
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/kanban/scripts/open_cards_brief.py:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/kanban/scripts/next_card_recommendation.py:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/hooks/card-ops/pc_kanban_card_ops.py:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/skills/chantier/scripts/pc_kanban_pending_user_action.py:*)
---

# Kanban — file-based board, agent-maintained

`_KANBAN/` (workspace root, cross-repo) is the live state of the work. **Epic = chantier, story =
the chantier's spec file, task = a card** that moves between column directories:

```
_KANBAN/
├── CHANTIER{NNNN}-<slug>/
│   ├── <SPEC>.md
│   ├── 0-ideas/              ← not yet scoped
│   ├── 1-todo/               ← scoped + committed
│   ├── 2-selected-for-dev/   ← commitment point — next session takes the highest `priority:`,
│   │                            file position tie-breaks
│   ├── 3-in-progress/        ← active — the card carries its handoff
│   └── 4-done/               ← done, waiting for its ADR — a card with no ADR to write skips it
└── _ARCHIVE/
    └── CHANTIER{NNNN}-done/
        └── YYYY-MM-DD_<card>.md  ← archive-date prefix, applied by `chantier`'s archiving step —
                                     see `chantier` skill § step 5
```

A card is a deliverable worth a multi-session handoff, never a micro-step (those are a checklist
*inside* the card). `0-ideas` → `1-todo` → `2-selected-for-dev` → `3-in-progress` → archive
(`mv` between column directories), through `4-done` only when the card needs an ADR first.

A card's slug — its file name without `.md`, the identifier the commands take — is at most 30
characters, so `/board` shows it whole. The `card-discipline` hook asks when a new card exceeds it;
existing cards are not renamed.

## The board's own table (`INDEX.md`)

`INDEX.md` (one per board) carries a `## Board` table — one row per chantier: id, objective, status.

**The status cell is the icon alone** — its legend is at the top of `INDEX.md`. The only text a
cell may carry is the blocker's chantier number, next to the blocked icon. No pointer, no
parenthetical, no PR / hash / date — that detail is in the chantier's story and cards, and a
session that needs it opens the folder. A cell that has grown a clause is over-cap by definition;
`stop_handoff.py --list-chantiers` truncates at 200 chars at read time, the fix is at write time.

## The story file

A fixed template — `## Objective` and `## Scope`, nothing else. Requirements, open questions and
steps live in the cards (§ Card body); `skills/chantier/scripts/story_view.py <chantier-dir>`
rebuilds the chantier-wide view from the open cards on demand. See
[references/story-template.md](references/story-template.md); read it before writing or pruning a
story.

## Card body

Markdown under the frontmatter. `## Why` and `## Done when` on every card; the three below only
when the card has something to put there — an empty section is left out:

- **`## Requirements`** — the table of the requirements this card defines (§ Requirements below for
  its form). A requirement may be defined in several cards provided the rows are identical
  (priority, statement, check); `story_view.py` warns when they differ.
- **`## Open questions`** — what only implementing this card can settle.
- **`## Steps`** — the order inside the card. An order between cards is `blocked_by`.

Archiving or dropping the card takes all three with it; nothing is retired from anywhere else.

## Card frontmatter

YAML, one card = one file. The body stays markdown prose (`## Done when`, `## Why`, narrative
handoff); frontmatter carries only what a script or a hook needs to resolve without parsing prose.

```yaml
---
date: YYYY-MM-DD       # meaning is implied by the column folder — see table below
priority: 1-5          # 1 (low) to 5 (urgent); omitted = 3
repos: []              # org/repo slugs (from git remote), e.g. [acme-corp/api-server]; [] or omitted = kanban-only
model: haiku | sonnet | opus | fable   # the floor this work needs — never a tier label to map
effort: low | medium | high | xhigh | max   # optional — how hard the worker reasons; absent = inherit the session
mockup: /abs/path | ~/path   # optional — a file, folder or .zip holding the mocked-up rendering
mockup_fidelity: [layout=strict, design=free]   # optional, needs mockup — axe=niveau items
sert: <parent-card-slug> | null        # what this card serves — null = autonomous root
requirements: []          # requirement ids this card traces to, e.g. [<id>, …]; [] = none
blocked_by: <card-slug> | null         # a hard dependency, never a mere ordering preference
---
```

The card's state **is** its column folder — there is no `status:` field to keep in sync. The
mapping is bijective (`0-ideas` ↔ idea, `1-todo` ↔ todo, `2-selected-for-dev` ↔ selected,
`3-in-progress` ↔ in-progress, `4-done` ↔ done); the board shows a card's state from its
folder name. A blocked card is not a state — it lives in `blocked_by:` plus a rendered badge.

- **`date`** — one field, meaning derived from the column folder:

  | column folder | what `date` means |
  |---|---|
  | `0-ideas` | surfaced |
  | `1-todo` | scoped |
  | `2-selected-for-dev` | selected |
  | `3-in-progress` | in progress since |
  | `4-done` | done |

  Set on creation; update only the value when a card moves column, never invent a second date field.

- **`priority`** — 1 (low) to 5 (urgent). The agent proposes a value at the same moment it writes a
  card (`kanban-proactivity.md` § Capture → card) with its reason; the human validates or changes
  it before the card is written — never agent-set unconfirmed. Drives the ordering of the next-card
  proposal in `0-ideas`/`1-todo` (`finishing-a-card.md` § Proposing the next card) and of the
  "highest `priority:`" pick in `2-selected-for-dev` above — `date:`/file position only tie-break
  at equal `priority`. Omitted = `3`, no backfill of existing cards, no error; `--list-stale`
  scales its threshold from it (`stop_handoff.py`). Independent of `sert` — a child never inherits
  its parent's `priority`, no auto-propagation.

- **`model`** — a floor, never a ceiling: the minimum a session needs before taking this card.
  A direct model alias (`haiku`/`sonnet`/`opus`/`fable`), never a tier name to resolve through a
  mapping table — the alias already floats to the latest release of that name natively, so there is
  nothing to keep in sync. Mandatory from `selected` onward; missing on a selected card → propose
  one and say why, never guess silently. A heterogeneous card tiers its bulk on `model:` and
  overrides outlier checklist items inline (`- [ ] (opus) arbitrate the placement`).

- **`effort`** — optional, the reasoning effort a worker is asked for: `low`, `medium`, `high`,
  `xhigh` or `max` (no `auto`). Absent = nothing imposed, the worker inherits the session's.
  `select` refuses any other value. `spawn-workers` passes it to the `Agent` call (and
  `claude --effort` on `--cmux`); `--effort` overrides it at call time.

- **`mockup`** / **`mockup_fidelity`** — optional; a card whose rendering was mocked up (HTML POC,
  screenshot, design) names the mockup as an absolute or `~/` path to a file, a folder or a `.zip`.
  The mockup is a throwaway working document that lives where it was dropped (`_TEMP/` included);
  the delivered work becomes the reference at closure. `mockup_fidelity` is a list of `axe=niveau`
  items, one flat key. Axes: `layout` (zones, order, heights, button rows), `design` (colour, font,
  icons), `content` (texts, labels), `behavior` (keyboard, hover, states, search). Levels: `strict`
  (reproduce identically), `guided` (follow the intent, justify every deviation in the report),
  `free` (inspiration only). An axis not listed is `guided`. `select` refuses a path that does not
  exist, an axis or level outside these lists, and a `mockup_fidelity` without `mockup`.
  `spawn-workers` puts the path and the levels in the worker's brief — it unpacks a `.zip` into the
  sibling folder `<zip name>/`, created if absent and reused otherwise — and asks for a
  `mockup element → implementation → status` table (status `done | deviation | missing`) on the
  `strict` and `guided` axes; a `strict` axis that cannot be met is reported, not silently
  deviated from. Only the inline `[a=b, c=d]` form of `mockup_fidelity` is read — a block-style
  (`- item`) list is not. The brief and the unpacking exist on the default path only, not on
  `--cmux`. The review before closing receives the same path and levels (`finishing-a-card.md`).

- **`repos`** — the repo-scope a session needs for this card, as `org/repo` slugs derived from each
  repo's `git remote get-url origin` — never a bare directory name. A name collides across orgs (see
  § Orgs below); the remote does not. Normalize with
  `sed -E 's#^(git@|https://|http://)[^:/]+[:/]##; s#\.git$##'` — covers SSH and HTTPS on any forge,
  host-agnostic. `/chantier`, on taking the card into `3-in-progress`, resolves each slug to an
  absolute path (`skills/chantier/scripts/resolve_repos.sh`, bounded `find` for `.git` + remote match
  — 0 or >1 candidates is reported, never guessed) and calls `permission-change freeze` with the
  results — write access is confined to these repos for the session's duration, PLUS every kanban
  board root (`stop_handoff.py --list-kanbans`, itself multi-board aware) so the session can still
  maintain cards/handoffs while frozen — deliberately write-only, never a read restriction (an
  installer tracking its own architecture decisions may want to record why). `[]`/omitted →
  kanban-only work, no repo touched.

- **`sert`** — what this card serves, a card slug (filename without `.md`) or `null`. Mandatory
  from `idea` onward. `null` marks an autonomous root — the objective itself, not a step toward one.
  A card serving two objectives is not a case this schema handles; split it. This field makes the
  critical path a render off the board instead of a hand-maintained list: nothing enumerates what a
  parent needs, every child declares what it serves.

- **`requirements`** — the requirement ids this card traces to, as a list; `[]` or omitted = none. Orthogonal to `sert`: `sert` is card-to-card filiation (critical
  path), `requirements` is card-to-requirement traceability, and a card may trace to several
  requirements where `sert` allows only one parent. A requirement id is only valid if an open card
  of the chantier defines it in its `## Requirements` table with a verification criterion — this
  card or another — never invent an id. `move-card select` refuses an id defined nowhere.

  **A chantier earns its requirements when it is reopened, never in a campaign.** Backfilling this
  field across a sleeping chantier assumes its requirements are already stated; where they are not,
  that is not a migration but the product work of writing the spec — and a table written for
  work nobody is doing goes stale before it is read. So the requirements come from the interview that
  reopens the chantier (skill `grill`), and its cards are backfilled in the same session, while the
  content is loaded. Leave the field omitted until then; omitted and `[]` mean the same thing.

  What a requirement has to be for the trace to mean anything — id format, the five families, the
  four invariants, the table's columns — is the § Requirements of this skill. See
  [references/requirements.md](references/requirements.md), and read it before writing or auditing
  a card's table.

- **`blocked_by`** — a hard dependency only, never a preference. Position inside
  `2-selected-for-dev` already carries ordering preference; conflating the two loses the distinction
  between "blocked" and "not yet prioritized".

## Handoff — two layers

- **Narrative (yours).** Prose in the in-progress card — state, decisions, next step. Your job, and
  the part that matters.
- **Auto block (the `Stop`/`SessionEnd` hook).** `stop_handoff.py` maintains a managed
  `<!-- auto-handoff:start … end -->` block at the bottom of in-progress cards whose `repos:` (or
  legacy `**Repos:**`) matches the session's repo, with the mechanical state (branch, uncommitted
  files, recent commits) whenever a turn ends with a dirty git tree. Non-blocking and idempotent —
  **never hand-edit between the markers**, it gets overwritten on the next run.
- **Anti-secrets, both layers.** The hook scrubs secret-shaped strings from what it writes; the
  narrative must follow the same directive — never put secrets, credentials or internal URLs in a
  handoff, in either layer.

## Finishing a card

A session's capacity limit is the session itself — one card, or one `sert`-linked group, per
session — and `/goal` is set from the group's **root** card, never the leaf. A `## Done when` line
must therefore be demonstrable in the transcript, and the reviewer must not be the generator.

See [references/finishing-a-card.md](references/finishing-a-card.md) for how the root is resolved
and the reviewer preference order — read it when taking a card in, or before declaring it done.

## Filiation as render, not list

`/chantier` groups queued cards by `sert` in its report — one group per root, cards with `sert: null`
elsewhere folded into a single "N autonomous cards" count. Never hand-maintain a critical-path list
on a parent card; if the render is wrong, a child's `sert` value is wrong, fix the child.

`sert` has a second, orthogonal use: at intake (`/chantier` step 7, first move into
`3-in-progress`), a non-null `sert` whose parent is `done` or already archived gets its
`## Done when` / `## Why` appended to the child as a managed block — content, not just grouping. See
`chantier`'s SKILL.md § step 7 for the mechanism; this section only flags that the field now
carries both.

## DRY across cards

A card that names another card links by path and stops — never restates its status, findings, or
design. When substantially editing a card, reduce any other card's pointer to it down to a bare
link, in the same edit.

## Orgs — a board can span more than one

**Wherever a `_KANBAN/` root sits on a drive shared with other people, it is a replication
boundary** — everything inside it lands on every machine that mounts it. **No secret goes anywhere
inside a shared root**; secrets stay outside (local-only / `.env`). Wherever this applies at all, it
is not negotiable by convenience.

See [references/multi-board.md](references/multi-board.md) for how several boards are mounted and
discovered — read it on a workspace that mounts more than one.

## Cross-agent log

`_AGENT_LOG/` (nested under `_KANBAN/`) is the async narrative channel between sessions/agents sharing an
org. A board still on the pre-nesting layout (a real sibling `_AGENT_LOG/` next to `_KANBAN/`, no
nested one yet) needs a one-time run of `hooks/agent-log-migration/pc_kanban_migrate_agent_log.py
--kanban <path-to-_KANBAN>` — `agent_log_dir()` (`stop_handoff.py`) resolves the sibling in the
meantime, so nothing breaks before that run, but the migration itself never happens on its own.
What a session did, cross-chantier, that no single card's handoff would hold. One file per
`(date, agent)`, append-only, filename `YYYY-MM-DD_<tool>-<agent-identity>.md`. `<agent-identity>` is
`git config user.name` (already declared for real reasons, no separate value to maintain). `<tool>`
names which agentic tool wrote the entry — needed because more than one tool can share the same git
identity and write to the same log. **Derive it from the running tool's own environment, never
hardcode one tool's name**: for Claude Code, `$AI_AGENT` (e.g. `claude-code_2-1-261_agent` → the
segment before the first `_`); another tool exposes its own equivalent variable. This file is
tool-agnostic by policy (see the marketplace's own `CLAUDE.md`) — a skill that only ever wrote
`claude-code-` would silently mislabel every entry from any other tool. Read the recent entries at
session start alongside the board; write into the org whose board carries the chantier worked on.
Never log secrets, credentials or internal URLs.

## Session write-scope — worker vs orchestrator

The git-repo boundary is the role boundary — the session is bound to its repo, or it is not:

- **Worker** — cwd **inside a git repo**, taking cards and editing code. Its freeze is a
  **per-session toggle**: `/chantier` step 7 resolves the card's `repos:` and calls
  `permission-change freeze <repos> <board roots>` at intake. No card taken, no freeze.
- **Orchestrator** — a session opened **outside any git repo** (workspace root, a repo group), for
  board curation and spawning workers; it never edits code. The `orchestrator_freeze.py`
  SessionStart hook detects this (cwd in no git repo, at least one board mounted) and freezes
  writes to the mounted boards' card + agent-log dirs with `careful` on — **derived, not
  declared**. A session opened inside a repo is a worker and the hook leaves it alone. To run an
  unfrozen session at a board root, `permission-change freeze off` (it returns next SessionStart).

Spawning workers for a selected card is skill `spawn-workers` — one `cmux new-workspace` per entry
of the card's `repos:`, each booting into `/pc-kanban:chantier`. Multi-repo cards relay
sequentially between workers; see `references/finishing-a-card.md` § "Relaying a multi-repo card".

## Boundaries

- Durable facts (user profile, conventions, contract constraints) live in the agent's persistent
  memory, not here — `_KANBAN/_FACTS.md` is a human-readable mirror, not the source.
- A file open in an editor is not a task; do not fold IDE context into a card.
- A finished card with no ADR to write is archived straight from `3-in-progress/` (skill
  `move-card`, verb `archive`). `4-done/` holds only a card that carries a durable decision — a
  rejected alternative written nowhere else (code, skill, existing ADR) — until its ADR exists.
  Nothing goes into the story: it holds the objective and scope, never a card's history.
  Archiving moves the card to `_ARCHIVE/CHANTIER{NNNN}-done/`, prefixed with the archive date —
  `YYYY-MM-DD_<filename>.md`, underscore, computed at archive time, not the card's own `date:` field
  (still "done since"). Never a bare `mv`; see `chantier` skill for the archiving mechanics.
