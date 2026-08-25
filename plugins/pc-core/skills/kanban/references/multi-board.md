# Multi-board — a session can mount more than one `_KANBAN/` root

Referenced from `SKILL.md`. Read this on a workspace mounting several boards. The replication
and anti-secret rule that goes with it stays in `SKILL.md`, where it is read every time.

Nothing here requires it, but the mechanism does not assume a single board either: a session can
mount more than one `_KANBAN/` root, each optionally paired with its own `_AGENT_LOG/` and knowledge
corpus. How repos are laid out inside the workspace is a separate, subjective choice per installer —
this mechanism makes no claim about it either way.

`stop_handoff.py --list-kanbans` is what actually finds every board a session may write to — the
nearest one, plus any additional root a machine-local config declares. Treat that config's exact
shape as an implementation detail, not something to encode here: it is expected to change (e.g. to
read multi-root editor config instead of a bespoke file) without altering anything else in this
skill.
