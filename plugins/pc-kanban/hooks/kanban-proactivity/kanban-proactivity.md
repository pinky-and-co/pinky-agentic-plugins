# Kanban — maintain the board proactively

A file-based kanban may carry the live state of the work (chantiers, cards moving between columns).
It replaces a ticketing tool and only works because **the agent maintains it reliably** — the user
stays the decider, the clerical layer is automated. See the `kanban` skill for the mechanics
(columns, dating, tier, filiation, cross-agent log).

Do not wait for "create a card" or "move it to done". Surface the board update as part of the work:

- **Capture → card.** A card exists for a reader who was not in this conversation — a later
  session, or a worker the orchestrator hands it to. What is surfaced and handled right here
  crosses no session boundary: no card. What is still open when this session ends is a card,
  side-quests included; write it without being asked. If it shares the subject of a card in
  progress, make it a `sert` child of that card instead of a new root. Its slug (file name without
  `.md`) is at most 30 characters; the `card-discipline` hook asks beyond that.
- **Select → commit.** The commitment column is a user decision by default; the agent may *propose*
  a selection, never select unasked.
- **Progress → move.** Taking a card in goes through the `chantier` skill (its step 7,
  in place when already loaded), never a hand move — it moves the card to in-progress and
  routes the implementation to the `card-worker` agent. Finished
  and verified means archiving it (`move-card archive`), straight from in-progress — through done
  only when it needs an ADR first.
- **End of session → handoff.** Before context runs out or the session ends, update the in-progress
  card's handoff so the next session resumes cleanly — narrative first, mechanical state second.
- **Narrate → log.** Cross-chantier work another session needs to know about goes in
  `_KANBAN/_AGENT_LOG/YYYY-MM-DD_<tool>-<agent-identity>.md` — write it without waiting to be asked, same discipline
  as the card handoff above.
