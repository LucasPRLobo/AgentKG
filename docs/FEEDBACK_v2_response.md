# agent-kg — Architect response to v2 heavy-use feedback (2026-06-11)

Context: first substantive consumer feedback from an agent that used agent-kg
heavily in one project. This doc maps each item to current build state and
recommends sequencing. Companion implementation board: `TASKS_v3.md`.

## Headline

Best kind of feedback — heavy user, candid, and it independently re-derives the
roadmap we reached through research. That convergence is a signal: the gaps a real
consumer hit are the same ones the schema research flagged and the same ones we
explicitly reserved foundations for. **Most of this is closer than the P0/P1/P2
framing makes it look.**

## The reframe: built / reserved / decided

| Feedback item | Actual state | Effort |
|---|---|---|
| #5 First-class `supersede` | **Already written** — `db.supersede(old_id, new_fact)` exists, sets `t_invalid` + links via `supersedes` column. Never exposed as an MCP tool. | Expose it. ~1h |
| #11 Recency/decay | **Already built** — `effective_confidence`, 90-day half-life, computed in every `recall`. Just invisible to the agent. | Surface it. Trivial |
| #3 Cross-project recall | `recall` filters `scope IN (?, 'global')`. `scope="all"` drops the clause. | Trivial |
| #8 `list_facts`/browse | Missing, but a plain SELECT. Highest value-per-effort. | Cheap |
| #9 Richer returns | Returns bare `{"result":"f10"}`; return the row dict. | Trivial |
| #2 Global profile | Mechanism exists (scope=global, `get_context.global_facts`). Missing = profile *concept* + nudge. | Medium |
| #1 Entities + relationships | **Reserved** `subject/predicate/object` columns in `facts`; edges layer deliberately deferred. | The real lift |
| #6 Typed facts | Taxonomy exists in file-based memory (`type: user\|feedback\|project\|reference`); add a `type` column + port. | Small |
| #4 dedup / #7 conflict / #10 digest | Variants of the deferred "semantic matching" decision; shared dependency. | Medium |
| #12 Multi-agent claims | Already specced as v3 coordination store. | Large, planned |

≈ Half the list is hours-to-days on foundations already laid.

## Recommended sequencing

**Wave 1 — cheap wins, remove daily friction (1–2 days):** expose `supersede` (#5),
`list_facts`/`list_observations` (#8), richer write returns (#9), `recall(scope="all")`
(#3), surface `effective_confidence`/age (#11). These fix what made the consumer
track IDs by hand and go blind after compaction.

**Wave 2 — typed facts + profile (#6, #2):** add `type` column, port the taxonomy,
define a first-class profile assembled from global `preference`/`user` facts that
`get_project_context` always returns. Typing is the backbone for both profile and
entity views.

**Wave 3 — entities/relationships (#1):** the genuine architectural decision. Right
direction (why we reserved the triple columns; validated by schema research), but
also where research flagged automated KG construction *fails* — identity resolution
is unreliable when LLM-mediated. Build it with "propose, don't auto-commit": entities
cheap to create, merges human/agent-confirmed, never auto-collapsed.

**Wave 4 — semantic/intelligence layer (#4, #7, #10):** dedup-on-remember, conflict
detection, end_session digest. Shared dependency on similarity matching; do once the
typed/entity foundation exists.

## The deepest point — organizing principle, above any single feature

The consumer's meta-observation:
> "The protocol lives in prose… I violated several silently… the system should turn
> these from documented norms into surfaced guardrails."

This is the **"agents are unreliable narrators"** principle that drove v1 hardening
and the UUID-scar fix. We already proved it: when the server validated and failed
loudly, the agent self-corrected; when it relied on prose, data was silently lost.
Every norm in the CLAUDE.md protocol that an agent can violate silently is a server
feature waiting to be built:
- "put user-level facts in global" → detect likely-global statements and nudge / default scope by type.
- "check for duplicates first" → surface near-duplicates before writing (#4).
- "call get_context first" → empty project returns an explicit "new project, no prior context" marker.

Frame this as the organizing principle of the next phase, not a feature.

## Caution

Don't do entities (#1) first because it's exciting. Wave-1 ergonomics are what make
the system usable enough to keep generating feedback like this. `list_facts` +
`supersede`-as-a-tool alone would have eliminated half of what the consumer
struggled with, for almost no cost. Earn the entity layer with the cheap wins first.

## Inert-syntax note
The consumer imported `[[wikilink]]` syntax from the file-based memory convention; in
agent-kg it's inert text (not parsed into edges). Either document supported syntax in
the `remember` tool description, or — better, once entities exist — parse `[[...]]`
into entity links.
