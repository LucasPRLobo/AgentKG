# agent-kg — reusable CLAUDE.md memory protocol (v3)

Copy the block below into any project's `CLAUDE.md`. Replace `<PROJECT>` with the
project's name (use the SAME string every time for a given repo — it's the key that
accumulates that project's history). Strategy notes for different project styles
follow the block.

---

## Block to copy

```markdown
# Memory protocol (agent-kg)

This project uses **agent-kg**, an MCP server providing persistent memory that
survives across sessions and is SHARED across all projects. Project key for every
tool call: **`<PROJECT>`**

agent-kg stores three things, and using the right one matters:
- **Observations** — the raw, append-only session log. What happened, what you
  found, what you decided, mid-work. Cheap, high-volume, timestamped.
- **Facts** — durable, curated knowledge meant to outlive the session: settled
  decisions, conventions, constraints, preferences. Lower-volume, long-lived,
  confidence-ranked (with decay), point-in-time queryable, and TYPED.
- **Entities** — shared nodes (a person, paper, project, file, concept) that facts
  attach to. Global by default, so "David" or "PAC learning" is ONE node across
  every project — the basis of the cross-project view.

Facts/observations carry a **scope**: `<PROJECT>` (specific to this repo) or
`global` (user-level, applies across ALL projects). Facts also carry a **type**:
`preference | decision | constraint | reference | person | project | other`.

## START of every session — orient before acting
1. `get_project_context(project="<PROJECT>")` FIRST, before reading files. Returns:
   the user **profile** (global preferences — how the user works, shown in every
   project), recent session summaries (with status + dangling flags), recent
   observations, your `global_facts`, and this project's `project_facts`. If it
   returns `status: "new_project_no_prior_context"`, this project is new — nothing
   was missed.
2. Read what it returns and orient from it. Do not re-read source files already
   summarized there — avoiding redundant file-reading is a core purpose.
3. `start_session(project="<PROJECT>", objective="<one line>")`. You don't need to
   keep the returned id — other tools resolve the active session from the project
   name. If a session shows `status="open"` (dangling), resume it or close it.

## DURING the session — log decisions, not narration
- `record_observation(project="<PROJECT>", content="...")` when you decide something
  non-obvious, hit a wall, learn what a file/module is for, or produce a result a
  future session needs. Terse, decision-focused. Signal over volume.

## DURABLE knowledge — promote to facts (and tag entities)
- `remember(scope, statement, type="...", confidence=1.0)` for a settled fact.
  Use `scope="global"` + `type="preference"` for how-the-user-works facts (they
  populate the profile shown everywhere); a project name for project-specific facts.
  The response echoes the stored fact — CHECK the scope/type landed right. It may
  include a `hint` (looks user-level → consider global) or `similar` (existing facts
  that overlap — if this updates one, `supersede` it; if duplicate, `forget` one).
- Reference entities inline with `[[Name]]` or `[[type:Name]]` in a statement and
  they're auto-linked (e.g. "reviewed [[person:David Silver]] on [[PAC learning]]").
- `recall(query, scope=?, type=?, as_of=?)` — search facts ranked by decayed
  confidence. Scope modes: omit → global only; `"<PROJECT>"` → project + global;
  `"all"` → every project. `as_of` (ISO time) = what was believed in the past.
  Results carry `effective_confidence` + `age_days` (judge staleness).
- `list_facts(scope=?, type=?)` / `list_observations(project=?, session=?)` — BROWSE
  (enumerate) when you don't have a search term. Add `include_invalid=True` to see
  forgotten/superseded facts.
- `supersede(old_fact_id, new_statement)` — when a fact EVOLVED. Replaces it and
  keeps a structural link + point-in-time history. Prefer this over forget+remember.
- `forget(fact_id)` — a fact is no longer true. Soft-delete; history preserved.

## ENTITIES — the cross-project view
- `create_entity(name, type)` / `link_fact(fact_id, name, type, role)` — attach facts
  to shared nodes. Exact name+type reuses the node; a near-but-different name returns
  `candidates` — agent-kg NEVER auto-merges. If a candidate IS the same thing, call
  `merge_entities(from_id, to_id)` (non-destructive, reversible). `add_alias` for akas.
- `get_entity(name_or_id)` — EVERYTHING about an entity across every project, with
  `facts_by_scope` (which projects touch it). This is "show me all I know about X".
- `list_entities(type=?)` — browse.

## END of every session — write a handoff
- `end_session(project="<PROJECT>", summary="...")`. Write FOR THE NEXT AGENT: what
  changed, what's in progress, what's blocked, what's next. The response returns the
  session's observations + a `digest_hint` — before you stop, review them and
  `remember` any DURABLE facts so conclusions aren't buried in the raw log.

## Rules of thumb
- Raw note → record_observation. Settled truth → remember (with a type). When unsure, observe.
- Heed the response hints: `hint` (mis-scope), `similar` (possible dup → supersede/forget),
  `candidates` (possible same entity → merge), `digest_hint` (promote facts at session end).
- A recurring person/paper/project/concept → make it an entity so it's queryable everywhere.
- Don't remember speculation as fact (low confidence, or leave it an observation).
```

---

## Strategy notes by project style

**Long-running research / exploratory** — highest payoff. Invest in `end_session`
handoffs (these projects pivot). `remember` negative results ("X is a dead end").
Make recurring papers/people/concepts **entities** so `get_entity` gives a
cross-project view. Promote conclusions to facts so context stays scannable.

**Steady feature development** — `remember` conventions and architecture decisions
(type `decision`/`constraint`, project scope): "we use X for auth," "never touch Z."
Observations capture the *why* behind non-obvious choices.

**Short / one-off** — barely need facts. start_session → record surprises →
end_session with a summary.

**Cross-project / personal conventions** — `remember(scope="global",
type="preference", ...)` your durable working preferences once; they surface in every
project's `profile`. Keep the global set small and high-signal. People/tools you use
across projects → global entities.

**Multi-agent / parallel** (future) — current model resolves "active open session per
project"; give each agent a distinct objective. Proper claim/lock coordination is the
planned next layer.

Meta-tip: the system is only as good as the discipline of writing facts and tagging
entities. The failure mode is recording everything as observations and never promoting
durable conclusions, so `get_project_context` becomes noise. A handful of well-chosen
`remember` calls (typed, correctly scoped, entity-tagged) per session is what makes the
next session — in any project — start *smart*.
