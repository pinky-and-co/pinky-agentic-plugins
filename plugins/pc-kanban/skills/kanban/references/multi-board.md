# Multi-board — a session can mount more than one `_KANBAN/` root

Referenced from `SKILL.md`. Read this on a workspace mounting several boards. The replication
and anti-secret rule that goes with it stays in `SKILL.md`, where it is read every time.

Nothing here requires it, but the mechanism does not assume a single board either: a session can
mount more than one `_KANBAN/` root, each carrying its own nested `_AGENT_LOG/` and knowledge
corpus. How repos are laid out inside the workspace is a separate, subjective choice per installer —
this mechanism makes no claim about it either way.

`stop_handoff.py --list-kanbans` is what actually finds every board a session may write to — the
nearest one (its parent is the session's **primary root**), plus every board declared for that
exact primary root in `kanban_boards` of `~/.claude/settings.pinky.json` — the sole declaration
path; a `*.code-workspace`'s `folders[]` and the `KANBAN_ROOTS` env var are read only once, by
skill `init`, to migrate whatever a workspace already declared that way. Skill `init` writes and
migrates this key; hand-edit it directly for a board it did not migrate (see that skill's own
`SKILL.md` § "Declaring a board this skill did not migrate").
