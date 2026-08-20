---
name: kanban
description: Mechanics of the file-based kanban board used to track chantiers (epics) and cards (tasks) — column lifecycle, card frontmatter schema, filiation, cross-agent log. Use when reading, moving, or writing a `_KANBAN/CHANTIER*/**/*.md` card, or when a session needs to resolve which repos/model/goal a card implies.
user-invocable: false
---

# Kanban — file-based board, agent-maintained

`_KANBAN/` (workspace root, cross-repo) is the live state of the work. **Epic = chantier, story =
the chantier's spec file, task = a card** that moves between column directories:

```
_KANBAN/CHANTIER{NNNN}-<slug>/
├── <SPEC>.md
├── 0-ideas/              ← not yet scoped
├── 1-todo/               ← scoped + committed
├── 2-selected-for-dev/   ← commitment point — user-ordered, next session takes the FIRST card
├── 3-in-progress/        ← active — the card carries its handoff
└── 4-done/               ← done (airlock) — fold the "why" into the story/an ADR, then purge
```

A card is a deliverable worth a multi-session handoff, never a micro-step (those are a checklist
*inside* the card). `0-ideas` → `1-todo` → `2-selected-for-dev` → `3-in-progress` → `4-done` →
archive (`mv` between column directories).

## Card frontmatter

YAML, one card = one file. The body stays markdown prose (`## Fini =`, `## Pourquoi`, narrative
handoff); frontmatter carries only what a script or a hook needs to resolve without parsing prose.

```yaml
---
status: idea | todo | selected | in-progress | done
date: YYYY-MM-DD       # meaning is implied by status — see table below
repos: []              # org/repo slugs (from git remote), e.g. [pinky-and-co/pinky-bootstrap]; [] or omitted = kanban-only
model: haiku | sonnet | opus | fable   # the floor this work needs — never a tier label to map
sert: <parent-card-slug> | null        # what this card serves — null = autonomous root
blocked_by: <card-slug> | null         # a hard dependency, never a mere ordering preference
---
```

- **`date`** — one field, meaning derived from `status`:

  | `status` | what `date` means |
  |---|---|
  | `idea` | surfaced |
  | `todo` | scoped |
  | `selected` | selected (moved into `2-selected-for-dev`) |
  | `in-progress` | in progress since |
  | `done` | done |

  Set on creation; update only the value when a card moves column, never invent a second date field.

- **`model`** — a floor, never a ceiling: the minimum a session needs before taking this card.
  A direct model alias (`haiku`/`sonnet`/`opus`/`fable`), never a tier name to resolve through a
  mapping table — the alias already floats to the latest release of that name natively, so there is
  nothing to keep in sync. Mandatory from `selected` onward; missing on a selected card → propose
  one and say why, never guess silently. A heterogeneous card tiers its bulk on `model:` and
  overrides outlier checklist items inline (`- [ ] (opus) arbitrate the placement`).

- **`repos`** — the repo-scope a session needs for this card, as `org/repo` slugs derived from each
  repo's `git remote get-url origin` — never a bare directory name. A name collides across orgs
  (`kanban.md` § Orgs already flagged this for org identification); the remote does not. Normalize
  with `sed -E 's#^(git@|https://|http://)[^:/]+[:/]##; s#\.git$##'` — covers SSH and HTTPS on any
  forge, host-agnostic. `/chantier`, on taking the card into `3-in-progress`, resolves each slug to
  an absolute path (`skills/chantier/scripts/resolve_repos.sh`, bounded `find` for `.git` + remote
  match — 0 or >1 candidates is reported, never guessed) and calls `permission-change freeze` with
  the results — write access is confined to these repos for the session's duration, PLUS every
  kanban board root (`stop_handoff.py --list-kanbans`, itself multi-board aware) so the session can
  still maintain cards/handoffs while frozen (see ADR-0004, `pinky-bootstrap`: write-only,
  deliberately not a read restriction). `[]`/omitted → kanban-only work, no repo touched.

- **`sert`** — what this card serves, a card slug (filename without `.md`) or `null`. Mandatory
  from `idea` onward. `null` marks an autonomous root — the objective itself, not a step toward one.
  A card serving two objectives is not a case this schema handles; split it. This field makes the
  critical path a render off the board instead of a hand-maintained list: nothing enumerates what a
  parent needs, every child declares what it serves.

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

## `/goal`, posed from the root of a `sert` group

A session's real capacity limit is the session itself — one card, or one `sert`-linked group of
cards, per session. `/chantier` operationalizes this natively instead of relying on judgment:

- Card's `sert: null` (a root) → `/goal` is set from **this card's own** `Fini =` line.
- Card's `sert: <parent>` → walk up to the root card (`sert: null`) that this chain ultimately
  serves, and set `/goal` from **the root's** `Fini =` line, not the leaf's. The session may then
  freely work any card in that `sert` group — the group is the unit of session, not the single card.

Consequence: a `Fini =` line must be **demonstrable in the transcript** — a test result, an exit
code, a real diff pasted — never merely true. `/goal`'s evaluator reads only the conversation
transcript, never a file or a command it would run itself; an assertion without the underlying
output in the transcript fools it exactly the way an unverified sub-agent report would.

### Reviewing before declaring `Fini =` met

The reviewer must not be the generator — a same-model self-check inherits its own blind spots,
and agreement among voices of one model is not proof of correctness (the exact reason
`council-tech`'s chairman-bis exists). Whenever a card's work needs real judgment reviewed, not
just a mechanical check, prefer in this order — **never `haiku`, whatever tier is available**:

1. **`adversarial` MCP tool** (`pc-core`, native, no spawn) — a genuinely different model family
   (Mistral-served, `mistral-medium-3.5` today), not just a cheaper same-vendor tier. The real
   change of perspective, not a cost optimization.
2. **`Agent(subagent_type: "reviewer")`** — `pc-core`'s own agent (`plugins/pc-core/agents/
   reviewer.md`), preloads the `review-guidelines` skill, no external plugin dependency. Same
   vendor as the generator, but a fresh context that never saw the generating session's own
   rationalizations — `model: sonnet` in its own definition, a real floor.
3. **Same-session self-review** — last resort only, and **say plainly that it is degraded** (same
   model, same context) rather than presenting it as equivalent to (1) or (2).

## Filiation as render, not list

`/chantier` groups queued cards by `sert` in its report — one group per root, cards with `sert: null`
elsewhere folded into a single "N autonomous cards" count. Never hand-maintain a critical-path list
on a parent card; if the render is wrong, a child's `sert` value is wrong, fix the child.

## Orgs — one workspace, several mounted orgs

A workspace is multi-org. An org is a GitHub organization mirrored locally, and it owns three
coordination artefacts under a single root: `_KANBAN/` (this board), `_AGENT_LOG/` (below), and its
knowledge corpus. **The org root is not where its repos live** — repos stay local to the workspace;
only the coordination surface may sit on a drive shared with other people.

**A shared org root is a replication boundary** — everything inside it lands on every machine that
mounts it. **No secret goes anywhere inside a shared root**; secrets stay outside (local-only /
`.env`). This is the one rule in this file that is not negotiable by convenience.

Board roots are currently resolved from a machine-local `.pinky/config.txt` (`kanban_roots_local`
key, `stop_handoff.py --list-kanbans`) — flagged legacy, slated for replacement by reading the
workspace's own `*.code-workspace` file instead (a user has no choice but to keep that one accurate,
since it is how their editor already sees every mounted root — see the parked idea card
`pinky-config-txt-remplace-par-code-workspace`). Don't build more on top of the current mechanism
than this file already needs.

## Cross-agent log

`_AGENT_LOG/` (next to `_KANBAN/`) is the async narrative channel between sessions/agents sharing an
org — what a session did, cross-chantier, that no single card's handoff would hold. One file per
`(date, agent)`, append-only, filename `YYYY-MM-DD_<agent-identity>.md`. Read the recent entries at
session start alongside the board; write into the org whose board carries the chantier worked on.
Never log secrets, credentials or internal URLs.

Not the only thing in `_AGENT_LOG/` — a Cortex (Snowsight) session writes a per-session **transfer
manifest** there instead (`YYYYMMDD_HHMISS_CORTEX_SESSION.md`): the file list for the user to
download, plus card moves Cortex cannot perform itself. The per-(date, agent) rule above does not
apply to it — the two artefacts coexist, not variants of each other.

## Boundaries

- Durable facts (user profile, conventions, contract constraints) live in the agent's persistent
  memory, not here — `_KANBAN/_FACTS.md` is a human-readable mirror, not the source.
- A file open in an editor is not a task; do not fold IDE context into a card.
- `4-done/` is an airlock: once a card's rationale is folded into the story or an ADR, move it to
  `_ARCHIVE/CHANTIER{NNNN}-done/`. A card not yet folded stays in `4-done/`.
