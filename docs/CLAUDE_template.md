# agent-kg — reusable CLAUDE.md memory protocol

Copy the block below into any project's `CLAUDE.md`. Replace `<PROJECT>` with the
project's name (use the SAME string every time for a given repo — it's the key that
accumulates that project's history). Strategy notes for different project styles
follow the block.

---

## Block to copy

```markdown
# Memory protocol (agent-kg)

This project uses **agent-kg**, an MCP server providing persistent memory that
survives across sessions. Project key for every tool call: **`<PROJECT>`**

agent-kg stores two kinds of things, and using the right one matters:
- **Observations** — the raw, append-only session log. What happened, what you
  found, what you decided, mid-work. Cheap, high-volume, timestamped.
- **Facts** — durable, curated knowledge meant to outlive the session: settled
  decisions, conventions, constraints, preferences. Lower-volume, long-lived,
  confidence-ranked, and queryable point-in-time.

Facts and observations each have a **scope**: `<PROJECT>` (specific to this repo)
or `global` (user-level knowledge that applies across ALL projects).

## START of every session — orient before acting
1. Call `get_project_context(project="<PROJECT>")` FIRST, before reading files.
   Returns: recent session summaries (with status + dangling flags), recent
   observations, your `global_facts`, and this project's `project_facts`.
2. Read what it returns and orient from it. Do not re-read source files already
   summarized there — avoiding redundant file-reading is a core purpose.
3. Call `start_session(project="<PROJECT>", objective="<one line>")`. You don't
   need to keep the returned id — every other tool resolves the active session
   from the project name automatically.
4. If a session shows status="open" (dangling), it was left unclosed. Resume it
   or close it with a summary before starting fresh.

## DURING the session — log decisions, not narration
- `record_observation(project="<PROJECT>", content="...")` when you decide
  something non-obvious, hit a wall, learn what a file/module is for, or produce
  a result a future session would need.
- Terse and decision-focused. Signal over volume — these are read at the start of
  every future session.

## DURABLE knowledge — promote to facts
- `remember(scope="<PROJECT>", statement="...")` for a settled project fact.
  `confidence` defaults to 1.0; lower it for tentative facts.
- `remember(scope="global", statement="...")` for user-level knowledge that should
  surface in EVERY project (working style, conventions, preferences). Use sparingly.
- `recall(query="...", scope="<PROJECT>")` to look up "what did we decide about X".
  Scope="<PROJECT>" also returns global facts. Pass `as_of="<ISO timestamp>"` for
  point-in-time ("what did we believe before X was reversed").
- `forget(fact_id="...")` when a fact is no longer true (soft-delete; history kept).
- Superseding a decision = new `remember` + `forget` of the old one.

## END of every session — write a handoff
- `end_session(project="<PROJECT>", summary="...")`. Write FOR THE NEXT AGENT:
  what changed, what's in progress, what's blocked, what's next.

## Other tools
- `search_memory(query="...")` — full-text over raw observations + session text.
- `ping()` — health check.

## Rules of thumb
- Raw note → record_observation. Settled truth → remember. When unsure, observe.
- Re-derived something you worked out before? It should have been a fact — remember
  it now so next time it surfaces automatically.
- Don't remember speculation as fact (low confidence, or leave it an observation).
```

---

## Strategy notes by project style

**Long-running research / exploratory** — highest payoff. Invest heavily in
`end_session` handoffs (these projects pivot). `remember` your negative results
("X is a dead end") — expensive to rediscover. Promote conclusions to facts so
`get_project_context` stays scannable as observations pile up.

**Steady feature development** — lighter touch. `remember` conventions and
architecture decisions (project scope): "we use X for auth," "never touch Z
directly." Observations capture the *why* behind non-obvious choices.

**Short / one-off** — barely need facts. start_session → record surprises →
end_session with a summary. Value is "if I come back in a month, I know where I left
off."

**Cross-project / personal conventions** — this is what `global` scope is for.
`remember(scope="global", ...)` durable preferences once; they surface in every
project. Keep the global set small and high-signal.

**Multi-agent / parallel** (future) — current model resolves "active open session
per project." With 2+ agents on one project, give each a distinct objective and
pass session id explicitly to avoid collision. (Proper claim/lock coordination is
the planned v3 layer.)

Meta-tip: the system is only as good as the discipline of writing facts. The failure
mode is recording everything as observations and never promoting durable
conclusions, so `get_project_context` becomes noise. A handful of well-chosen
`remember` calls per session is what makes the next session start *smart*.
