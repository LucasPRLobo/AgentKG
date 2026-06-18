# agent-kg v2 — Implementation Board

Knowledge-layer build. Spec: [`SCHEMA.md`](./SCHEMA.md). Research backing: memory `schema_research.md`.

**Decisions locked for this build** (per "earn its place" discipline):
- Auto-promotion DEFERRED — ship `remember()` (explicit assertion) only; no automatic observation→fact promotion yet.
- Fact matching: exact/keyword now, semantic later.
- Facts use `statement` only; `subject/predicate/object` columns reserved but unused.
- Decay half-life: 90 days, as a tunable constant.

**Legend:** ☐ todo · ◐ in progress · ☑ done · ⚠ gotcha · → depends on

**Critical path:** M1-1 → M1-3 → M2-2 → M2-4 → M3-1 → M3-5

**Progress:** M1 ✅ & M2 ✅ COMPLETE — full knowledge layer built & verified (migration, models, facts+provenance, decay, recall point-in-time, remember/forget/supersede). 🎉 **v2 SHIPPED** — M1 ✅ M2 ✅ M3 ✅ M4 ✅ all complete & deployed live. Knowledge layer operational in production: bi-temporal facts, provenance, decay-ranked recall, point-in-time, scope/portability, string IDs, project-based session lifecycle. Smoke-tested against real server. Remaining work is all in the "Carry-forward / follow-ups" section below (none blocking). Next major phase: v3 coordination store (trigger: 2+ concurrent agents). Counters table in M1-3; db.py uses `from datetime import datetime, timezone`.
**Carry-forward:** multi-statement writes (`create_fact`, `supersede`) still rely on caller rollback — optionally wrap in `with conn:` during M3.
**Carry-forward note:** consider wrapping multi-statement writes (`create_fact`, future `supersede`) in `with conn:` so a mid-write failure auto-rolls-back instead of leaving a pending transaction that a later `commit()` could flush. See M2-2 verification.

---

## M1 — Migration & Foundation ✅ COMPLETE
*Do first. Everything depends on this. Prove against the real DB before building on it.*

### ☑ M1-1 · Additive schema migration in `init_db`
**File:** `src/agent_kg/db.py` (extend existing `init_db`)
**Code:**
- After the current `CREATE TABLE IF NOT EXISTS` block, add `CREATE TABLE IF NOT EXISTS` for `facts` and `fact_sources` (fields per SCHEMA.md §3.2, §3.3).
- Add guarded `ALTER TABLE` for new columns — SQLite has no `ADD COLUMN IF NOT EXISTS`, so check first:
  ```python
  def _has_column(conn, table, col):
      return any(r["name"] == col for r in conn.execute(f"SELECT name FROM pragma_table_info('{table}')"))
  ```
  Add only if missing: `observations.scope` (TEXT), `observations.agent_id` (TEXT), `observations.promoted_to` (TEXT), `nodes.status` (TEXT).
- ⚠ Keep `PRAGMA foreign_keys=ON` working — `fact_sources` has FKs to both `facts` and `observations`.
**Done when:** `init_db` on a fresh DB and on a v1 DB produce identical schema; running twice is a no-op.

### ☑ M1-2 · Backfill existing data → M1-1
**File:** `src/agent_kg/db.py` (helper called from `init_db`, idempotent)
**Code:**
- `observations.scope`: backfill via join `session_id → nodes.project_id → project label`. Guard with `WHERE scope IS NULL`.
- `nodes.status`: `UPDATE nodes SET status = CASE WHEN body IS NULL THEN 'open' ELSE 'closed' END WHERE type='session' AND status IS NULL`.
- Leave `agent_id` NULL (unknown for historical rows).
**Done when:** `SELECT count(*) FROM observations WHERE scope IS NULL` → 0; dangling sessions show `status='open'`.

### ☑ M1-3 · ID-minting helper → M1-1
**File:** `src/agent_kg/db.py`
**Code:**
- New table `counters(scope TEXT, kind TEXT, n INTEGER, PRIMARY KEY(scope, kind))`.
- `mint_id(conn, kind, scope) -> str` where `kind ∈ {'o','s','f'}`. Increment `n` and read it back **in the same transaction as the caller's insert** so it's serialized through SQLite's single writer.
- Format (SCHEMA.md §2): observation `f"{scope}/o{n}"`, session `f"{scope}/s{n}"`, fact `f"f{n}"` (fact scope counter is global).
**Done when:** 100 rapid mints are unique + sequential; counter survives a restart.

---

## M2 — Knowledge Layer (`db.py`) ✅ COMPLETE
*Core storage logic. → M1 complete.*

### ☑ M2-1 · `Fact` model + extend `Observation`
**File:** `src/agent_kg/models.py`
**Code:**
- New `Fact(pydantic.BaseModel)` mirroring `facts` (SCHEMA.md §3.2): `id, scope, statement, subject|None, predicate|None, object|None, t_created, t_invalid|None, valid_from|None, valid_until|None, confidence=1.0, recurrence_count=1, last_confirmed_at, supersedes|None, created_at, updated_at`. Temporal fields `datetime | None`.
- Add to `Observation`: `scope: str`, `agent_id: str | None = None`, `promoted_to: str | None = None`.
**Done when:** `Fact(**dict(row))` round-trips a DB row both ways.

### ☑ M2-2 · `create_fact` + provenance linking → M2-1, M1-3
**File:** `src/agent_kg/db.py`
**Code:**
- `create_fact(conn, fact: Fact, source_obs_ids: list[str]) -> None`:
  1. Insert into `facts`.
  2. Insert `(fact.id, obs_id)` into `fact_sources` for each source.
  3. `UPDATE observations SET promoted_to = fact.id WHERE id IN (...)`.
  - All one transaction.
**Done when:** every fact has ≥1 `fact_sources` row; each source's `promoted_to` points back.

### ☑ M2-3 · `effective_confidence` (decay model)
**File:** `src/agent_kg/db.py` (pure function, no DB)
**Code:**
```python
import math
LAMBDA = math.log(2) / 90  # 90-day half-life

def effective_confidence(base: float, last_confirmed_at: datetime, now: datetime) -> float:
    age_days = (now - last_confirmed_at).total_seconds() / 86400
    return base * math.exp(-LAMBDA * age_days)
```
**Done when:** fresh fact ≈ base; 90-day-stale fact ≈ base/2. Unit-testable in isolation.

### ☑ M2-4 · `recall` → M2-3
**File:** `src/agent_kg/db.py`
**Code:**
- `recall(conn, query, scope=None, as_of=None, limit=10) -> list[dict]`.
- Default `as_of = now`. Keyword/FTS match on `facts.statement`.
- ⚠ Validity filter (point-in-time): `WHERE t_created <= as_of AND (t_invalid IS NULL OR t_invalid > as_of)`.
- Scope filter: if `scope` given → `scope IN (?, 'global')`, else all scopes.
- Rank results by `effective_confidence(base, last_confirmed_at, as_of) * relevance` in Python after fetch.
**Done when:** a superseded fact is hidden by default but reappears when `as_of` is set before its `t_invalid`.

### ☑ M2-5 · `remember`, `forget`, supersession helper → M2-2
**File:** `src/agent_kg/db.py`
**Code:**
- `remember(conn, scope, statement, confidence=1.0, source_obs_ids=()) -> str`: mint fact id, build `Fact` (`t_created=now`, `last_confirmed_at=now`), `create_fact`. Return handle.
- `forget(conn, fact_id) -> None`: `UPDATE facts SET t_invalid = now WHERE id = ?` (soft; never DELETE).
- `supersede(conn, old_id, new_fact)`: set old `t_invalid = new_fact.t_created`; set `new_fact.supersedes = old_id`; create new. ⚠ Invoked explicitly only — no auto contradiction detection in v2.
**Done when:** `forget` hides from `recall` but row persists; `as_of` before the forget still returns it.

---

## M3 — MCP Tool Surface (`server.py`) ✅ COMPLETE
*Agent-facing tools. → M2 complete.*

### ☑ M3-1 · Stateless active-session resolution *(the UUID-scar fix)*
**File:** `src/agent_kg/server.py` (helper)
**Code:**
- `_resolve_session(conn, project, session=None) -> str`:
  - if `session` provided → validate it exists + is a session, else raise loudly.
  - else → `SELECT id FROM nodes WHERE type='session' AND project_id=(project's id) AND status='open' ORDER BY updated_at DESC LIMIT 1`.
  - if none → raise `ValueError(f"No open session for '{project}'. Call start_session first.")`.
- ⚠ Agents pass `project`, never a session UUID — that's the whole point. No in-memory state (stdio = one server per client; "latest open session in DB" is correct + restart-safe).
- ⚠ Two concurrent open sessions on one project is ambiguous — acceptable for v2 single-agent; resolved in v3 via coordination claims. Note it in a comment.
**Done when:** record→observe→end works passing only `project`; no UUID is ever passed by the agent.

### ☑ M3-2 · Rework `record_observation` → M3-1
**File:** `src/agent_kg/server.py`
**Code:** Signature `(project: str, content: str, session: str | None = None) -> str`. Resolve via `_resolve_session`, `mint_id('o', project)`, set `scope=project`, `agent_id=None`, insert (reuse `create_observation`, which also updates FTS).
**Done when:** observation lands with correct scope + session link; loud error when no open session.

### ☑ M3-3 · `start_session` / `end_session` updates → M1-3, M3-1
**File:** `src/agent_kg/server.py`
**Code:**
- `start_session(project, objective)`: ensure project node; `mint_id('s', project)`; create session node with `status='open'`. Return handle.
- `end_session(summary, session=None, project=None)`: resolve session; set `body=summary`, `status='closed'`, `updated_at=now`. Return confirmation.
**Done when:** lifecycle flips `status`; an unclosed session stays `open` (dangling).

### ☑ M3-4 · `remember` / `recall` / `forget` tools → M2-4, M2-5
**File:** `src/agent_kg/server.py`
**Code:** Thin `@mcp.tool()` wrappers over the M2 functions.
- ⚠ Docstrings are load-bearing — they tell the agent *when* to call. E.g. `remember`: "Call when you learn a durable user preference or a project decision worth keeping across sessions." `recall`: "Search durable facts; pass as_of to query what was believed at a past time."
**Done when:** all three callable end-to-end from a Claude session.

### ☑ M3-5 · Upgrade `get_context` → M2-4
**File:** `src/agent_kg/db.py` + `server.py`
**Code:** Extend `get_context` to also return:
- `global_facts`: non-invalidated `scope='global'` facts, ranked by effective confidence.
- `project_facts`: non-invalidated `scope=project` facts, same ranking.
- keep existing `sessions` (with `dangling` + `obs_count`) and recent `observations`.
**Done when:** starting a session in *any* project surfaces global facts — portability payoff visible.

---

## M4 — Verify & Deploy ✅ COMPLETE

### ☑ M4-1 · Migration test on real-DB copy → M1
**Do immediately after each M1 card.** ⚠ The DB is WAL-mode — `cp` of the `.db` file alone misses the `-wal` sidecar and yields an EMPTY copy. Use `sqlite3 data/agent_kg.db ".backup /tmp/v1copy.db"` (or `VACUUM INTO`). Then run `init_db` on the copy, assert 47 observations / 8 sessions / 2 projects intact. Never test migrations on the live DB.
- M1-1 schema migration: ☑ verified (columns added, idempotent, data intact).
- M1-2 backfill: ☑ verified (0 NULL scopes, status backfilled, 3 dangling sessions flagged, idempotent).
- M1-3 mint_id + full module: ☑ verified on real-DB copy (migration + mint_id + get_context all green after file-scramble repair).

### ☑ M4-2 · End-to-end knowledge-loop test → M3
Temp DB, mirror the v1 hardening test style:
`start_session → record_observation(by project) → remember(global fact) → end_session → get_context from a DIFFERENT project shows the global fact → recall(as_of=past) proves point-in-time`.

### ☑ M4-3 · Restart + re-register → M3  *(DEPLOYED)*
Claude Code restarted; v2 server live. Smoke-tested against real DB: `get_project_context('desafio_quant')` returned `global_facts`/`project_facts` keys + sessions with `status`/`dangling`; `remember`→`recall`→`forget` round-trip confirmed (f1 minted, recalled with effective_confidence, soft-invalidated, hidden from recall). v2 fully operational.

---

## Carry-forward / follow-ups (post-v2)
- ☐ Wrap multi-statement writes (`create_fact`, `supersede`) in `with conn:` for auto-rollback (currently rely on caller). Minor hardening.
- ☐ Refresh `CLAUDE.md` memory-protocol template for new tool signatures (`record_observation`/`end_session` take `project`; new `remember`/`recall`/`forget`).
- ☐ Auto-promotion (observation→fact recurrence rule) — deliberately deferred; revisit after real fact usage.
- ☐ Semantic fact matching for recurrence/contradiction — deferred; exact/keyword for now.

---

## Deferred to v3 (do NOT build now)
Coordination store: `tasks`, `claims`, `leases`, State-Flag `OPEN→CLAIMED→DONE`, per-project `coordination.db`. **Trigger:** first time two agents run concurrently on one project. See SCHEMA.md §8.
