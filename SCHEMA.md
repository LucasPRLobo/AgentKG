# agent-kg v2 — Definitive Knowledge-Layer Schema

Status: **design spec, for review before implementation.** Supersedes the v1 flat model (additively — no data loss). Grounded in the 2026-06-10 verified research sweep (see memory `schema_research.md`).

---

## 0. The two-store split (decided)

Split by **liveness**, with **scope** as an attribute inside the knowledge store.

| Store | Holds | Lifetime | Build |
|-------|-------|----------|-------|
| **Knowledge** | observations, facts, session summaries — anything you'd *recall later* | durable | **v2 (now)** |
| **Coordination** | live task/claim/lease state — who's doing what *right now* | ephemeral | v3 (when 2+ concurrent agents) |

The line: **a session summary is knowledge; a session's live claim is coordination.**

`scope` on a knowledge record is either:
- `global` — user-level, cross-project (preferences, conventions, working style). **This is the vendor-portable wedge — the context that travels between tools/agents.**
- `<project handle>` — project-specific durable knowledge (e.g. `desafio_quant`).

This spec covers the **knowledge store only**. Coordination is sketched in §8 and deferred.

---

## 1. Design principles (from verified research)

1. **Two layers: raw → curated, with lineage.** Raw `observations` (append-only, immutable) get promoted to curated `facts`. Every fact traces back to the observations that produced it. *(Graphiti/Zep, 3-0.)*
2. **Bi-temporal facts.** Separate *transaction time* (when we recorded it) from *validity time* (when it's true in the world). *(Zep + ATOM + 2018 theory — most-validated finding.)*
3. **Invalidate, never delete.** Supersession marks the old record invalid; the row stays for point-in-time queries. *(Zep, 3-0.)*
4. **Human-readable string IDs, never UUIDs.** *(Official MCP memory server, 3-0 — and our own data-loss scar.)*
5. **Agents don't carry IDs.** The server tracks the active session per agent; agents pass a `project`, not a session handle. Kills the mistranscribed-UUID failure mode structurally.
6. **Fail loudly.** Unknown references raise corrective errors so the agent self-corrects. *(Already added in v1 hardening.)*
7. **Provenance is first-class.** Every record carries `agent_id` + `source`. Load-bearing the moment >1 agent writes.

---

## 2. ID scheme

All IDs are **server-minted, human-readable string handles**. Agents never invent them.

| Entity | Handle format | Example |
|--------|---------------|---------|
| session | `<project>/s<N>` | `desafio_quant/s7` |
| observation | `<project>/o<N>` | `desafio_quant/o42` |
| fact | `f<N>` or `f<N>-<slug>` | `f17-prefers-sqlite` |
| project | the bare handle | `desafio_quant`, `global` |

`<N>` is a per-scope monotonic counter held server-side. Collision-free without coordination because minting is serialized through one SQLite writer.

---

## 3. Tables

### 3.1 `observations` (raw layer — v1 table, extended)

```sql
CREATE TABLE observations (
    id          TEXT PRIMARY KEY,          -- 'desafio_quant/o42'
    scope       TEXT NOT NULL,             -- project handle | 'global'
    session_id  TEXT REFERENCES nodes(id), -- provenance anchor (nullable for direct global notes)
    agent_id    TEXT,                      -- provenance: who wrote it
    content     TEXT NOT NULL,
    promoted_to TEXT REFERENCES facts(id), -- set once promoted to a fact (NULL = still raw)
    created_at  TEXT NOT NULL
);
-- FTS5 over content unchanged from v1
```

Immutable after write. The append-only ground truth.

### 3.2 `facts` (curated bi-temporal layer — NEW)

```sql
CREATE TABLE facts (
    id              TEXT PRIMARY KEY,      -- 'f17-prefers-sqlite'
    scope           TEXT NOT NULL,         -- 'global' | project handle
    statement       TEXT NOT NULL,         -- curated fact text

    -- optional triple form, for future graph edges (nullable in v2)
    subject         TEXT,
    predicate       TEXT,
    object          TEXT,

    -- bi-temporal: transaction time (our store) vs validity time (the world)
    t_created       TEXT NOT NULL,         -- when WE recorded it
    t_invalid       TEXT,                  -- when superseded/retracted in our store (NULL = live)
    valid_from      TEXT,                  -- when true in the world (NULL = unknown)
    valid_until     TEXT,                  -- when it stopped being true (NULL = still/unknown)

    -- confidence + decay/recurrence (our design — §5)
    confidence       REAL NOT NULL DEFAULT 1.0,
    recurrence_count INTEGER NOT NULL DEFAULT 1,
    last_confirmed_at TEXT NOT NULL,

    supersedes      TEXT REFERENCES facts(id),  -- the fact this replaces (NULL)
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
```

`statement` is the flexible form we use now; `subject/predicate/object` are reserved so facts can become graph edges later without migration.

### 3.3 `fact_sources` (provenance lineage — NEW)

```sql
CREATE TABLE fact_sources (
    fact_id        TEXT NOT NULL REFERENCES facts(id),
    observation_id TEXT NOT NULL REFERENCES observations(id),
    PRIMARY KEY (fact_id, observation_id)
);
```

Every fact → the observations that produced it. (Graphiti's `episodes` field, normalized.)

### 3.4 `nodes` (sessions + projects — v1 table, one column added)

Sessions stay as the provenance anchor and handoff carrier. One column added for honest dangling detection (NOT full coordination):

```sql
ALTER TABLE nodes ADD COLUMN status TEXT;  -- 'open' | 'closed' (sessions only)
-- backfill: body IS NULL -> 'open', else 'closed'
```

Live claims/leases/tasks are explicitly **not** here — they're the v3 coordination store.

---

## 4. Promotion: observation → fact

The curated layer is built by promoting raw observations.

- **Trigger (confidence-and-recurrence):** when the same fact is observed `recurrence_count ≥ PROMOTE_THRESHOLD` (default 2) across observations without contradiction, OR on explicit `remember()`.
- **On promotion:** create a `facts` row, link every supporting observation via `fact_sources`, set `observations.promoted_to`.
- **Re-confirmation:** a later observation matching an existing fact bumps `recurrence_count`, refreshes `last_confirmed_at`, and raises `confidence` (capped at 1.0).
- **Human-in-the-loop stays available:** `remember()` asserts a fact directly (confidence 1.0); promotion rules handle the automatic path. *(Matches the "propose, don't auto-commit" decision in `architecture_decisions.md`.)*

---

## 5. Confidence + decay (our design — the research's open gap)

No surveyed system models time-based confidence decay (only static floats + hard TTL). Our model:

- **Stored:** `confidence` (base), `recurrence_count`, `last_confirmed_at`.
- **Effective confidence (computed lazily at query time):**
  ```
  effective = confidence * exp(-LAMBDA * age_days(last_confirmed_at))
  ```
  where `LAMBDA` is tuned so an unconfirmed fact halves at ~90 days (default — a hypothesis to validate with real usage).
- **Recurrence fights decay:** each re-confirmation refreshes `last_confirmed_at` and raises base `confidence`, so durable truths stay sharp while one-off guesses fade.
- **Decay never deletes.** It only lowers ranking. Stale facts sink; they don't vanish.

`recall` ranks by `effective_confidence × relevance`.

---

## 6. Supersession / contradiction (invalidate, never delete)

When a new fact contradicts an existing one (temporal-overlap on the same subject):

1. Set old fact's `t_invalid` = new fact's `t_created` (transaction time) and/or `valid_until` (validity time).
2. Insert the new fact with `supersedes = <old fact id>`.
3. Never `DELETE`.

**Point-in-time query** ("what did we believe on date T"):
```sql
WHERE t_created <= T AND (t_invalid IS NULL OR t_invalid > T)
```

`forget(fact_id)` is soft — sets `t_invalid = now`, never a hard delete.

---

## 7. MCP tool surface (v2)

| Tool | Signature | Notes |
|------|-----------|-------|
| `start_session` | `(project, objective) → session_handle` | server sets this as the agent's **active session** |
| `record_observation` | `(project, content, [session]) → obs_handle` | attaches to active session; agent passes **project, not a UUID**; validates loudly |
| `end_session` | `([session], summary) → confirmation` | defaults to active session; writes durable handoff |
| `remember` | `(scope, statement, [confidence]) → fact_handle` | direct fact assertion (e.g. a user preference) |
| `recall` | `(query, [scope], [as_of]) → ranked facts + observations` | point-in-time via `as_of`; ranks by effective confidence × relevance; hides invalidated |
| `get_context` | `(project) → {global facts, project facts, recent sessions (w/ dangling flags + obs_count), recent observations}` | the session-start hydrator |
| `forget` | `(fact_id) → confirmation` | soft-invalidate |
| `ping` | `() → "pong"` | unchanged |

`get_context` now returns **global-scope facts alongside project facts** — so starting a session in any project hydrates the agent with user-level knowledge (the portability payoff).

---

## 8. Deferred to v3 — coordination store (sketch, do NOT build yet)

Separate `coordination.db` per project. State-Flag schema from the stigmergy finding:

- `tasks`: id, objective, status `OPEN → CLAIMED → DONE`, depends_on edges
- `claims`: task_id, agent_id, lease_expiry — exclusivity via SQLite serialized transaction; expired lease auto-reverts (kills dangling-session problem structurally)
- claim predicate: `flag == OPEN AND agent.capable`
- contention mitigation: claim guards/leases; direct messaging reserved for hot paths

**Trigger to build:** first time two agents run concurrently on one project. Not before.

---

## 9. Migration from v1 (additive — zero data loss)

1. `observations`: add `scope` (backfill from `session → project`), `agent_id` (NULL), `promoted_to` (NULL).
2. Create `facts`, `fact_sources`.
3. `nodes`: add `status` (backfill from `body IS NULL`).
4. Existing 47 observations + 8 sessions + 2 projects preserved untouched.

No promotion is run retroactively — facts accrue from new usage forward (and optionally a one-time backfill pass later if wanted).

---

## 10. Open decisions before implementation

1. **`LAMBDA` / decay half-life** — 90 days is a guess. Acceptable to ship as a tunable default and revisit.
2. **`PROMOTE_THRESHOLD`** — 2 observations to auto-promote? Or only `remember()` for now, deferring auto-promotion until we see noise/signal in real facts.
3. **Fact matching for recurrence/contradiction** — exact-ish statement match in v2 (cheap), or LLM-judged semantic match (richer, slower, a dependency)? Lean: start exact/keyword, add semantic later.
4. **`statement` vs `subject/predicate/object`** — ship statement-only now, keep triple columns reserved? (Recommended.)
