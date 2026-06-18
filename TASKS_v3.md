# agent-kg v3 — Feedback Implementation Board

Source: heavy-use consumer feedback (see `docs/FEEDBACK_v2_response.md`, memory `usage_feedback_v2`).
Organizing principle: **every prose-protocol norm an agent can violate silently is a server feature** ("agents are unreliable narrators" — same principle as the v1 hardening / UUID-scar fix).

**Legend:** ☐ todo · ◐ in progress · ☑ done · ⚠ gotcha · → depends on
**Files:** `db.py` (storage), `server.py` (MCP tools), `models.py` (pydantic).
**Discipline (unchanged):** verify each card on a `.backup` copy of the real DB; migrations additive + idempotent; never test on the live DB.

---

## Wave 1 — Cheap wins (remove daily friction) ✅ COMPLETE (verified on real-DB backup 2026-06-18)
*These fix what made the consumer track IDs by hand and go blind after compaction. Mostly foundations already exist.*
*Shared helper added: `db._enrich_fact` (effective_confidence + age_days), reused by recall/list_facts/_rank_live_facts. New db fns: get_fact, get_observation, list_facts, list_observations.*

### ☑ W1-1 · Expose `supersede` as an MCP tool  *(logic already exists)*
**Why:** consumer did `forget`+`remember` manually; the structural link existed but wasn't reachable.
**File:** `server.py` (db.supersede already in `db.py`).
**How:**
- Add `@mcp.tool() def supersede(old_fact_id, new_statement, scope=None, confidence=1.0)`.
- Fetch old fact; default `scope`/`type` from it if not given. Build new `Fact` (mint id, `t_created=now`, `last_confirmed_at=now`). Call `db.supersede(conn, old_fact_id, new_fact)`.
- Raise loudly if `old_fact_id` unknown.
- Return the new fact row (see W1-3).
**Done when:** one call retires old + creates new with `supersedes` populated; `recall` shows only the new one; `as_of` before the change shows the old one.

### ☑ W1-2 · `list_facts` / `list_observations` browse tools  *(biggest day-to-day gap)*
**File:** `db.py` + `server.py`.
**How:**
- `db.list_facts(conn, scope=None, type=None, include_invalid=False, limit=50)`: `SELECT * FROM facts` with optional `scope`/`type` filters; default `WHERE t_invalid IS NULL`; order by `t_created DESC`. Attach `effective_confidence` (reuse `_rank_live_facts` pattern).
- `db.list_observations(conn, project=None, session=None, limit=50)`: join observations→session; filter by project label or session id; order `created_at DESC`.
- Thin `@mcp.tool()` wrappers.
**Done when:** an agent can enumerate everything it stored without guessing search queries; survives context compaction.

### ☑ W1-3 · Richer write returns  *(catch mis-scope immediately)*
**File:** `server.py`.
**How:** `remember`, `supersede`, `forget`, `record_observation` return the stored row (id, scope, type, statement/content, t_created) instead of bare `{"result":"f10"}`. Reuse a `_fact_row(conn, id)` helper.
**Done when:** the write response shows what actually landed (scope included) so a mis-scope is visible at write time.

### ☑ W1-4 · `recall(scope="all")` — cross-project recall
**File:** `db.py` `recall` + `server.py`.
**How:** in `recall`, if `scope == "all"`, omit the scope WHERE clause entirely (current default filters `scope IN (?, 'global')`). Document the three modes in the tool docstring: `None`=global only, `"<project>"`=project+global, `"all"`=everything.
**Done when:** `recall(query, scope="all")` returns matching facts across every project.

### ☑ W1-5 · Surface decay/age in outputs  *(decay already computed, just invisible)*
**File:** `db.py` (recall + list_facts).
**How:** ensure `effective_confidence` is in every fact return (already in `recall`); add `age_days` = days since `last_confirmed_at`. No new math — `effective_confidence` exists with 90-day half-life.
**Done when:** agents can see a fact is stale and decide to re-validate.

### ☑ W1-6 · Empty-project marker in `get_context`  *(guardrail)*
**File:** `db.py` `get_context`.
**How:** when project node absent AND no project facts/observations, add `"status": "new_project_no_prior_context"` to the return so the agent knows it isn't missing anything (rather than silently seeing empties).
**Done when:** first session in an empty project gets an explicit signal.

---

## Wave 2 — Typed facts + profile ✅ COMPLETE (verified on real-DB backup 2026-06-18)
*Typing is the backbone for profile + entity views + filtered recall.*

### ☑ W2-1 · Add `type` column to `facts`  → W1
**File:** `db.py` migration + `models.py`.
**How:** additive guarded `ALTER TABLE facts ADD COLUMN type TEXT`. Port file-memory taxonomy: `preference | decision | constraint | reference | person | project | other`. Add `type: str = "other"` to `Fact` model. Backfill existing facts to `other` (or NULL).
**Done when:** facts carry a type; migration idempotent; existing facts preserved.

### ☑ W2-2 · `type` in remember/recall/list  → W2-1
**File:** `server.py` + `db.py`.
**How:** `remember(..., type="other")`; `recall(..., type=None)` and `list_facts(..., type=None)` filter by it. Update docstrings to enumerate the taxonomy.
**Done when:** filtered recall by type works (e.g. all `decision` facts for a project).

### ☑ W2-3 · First-class profile in `get_project_context`  → W2-1
**File:** `db.py` `get_context`.
**How:** assemble a `"profile"` key from global facts of type `preference`/`user`, returned in EVERY project. This is the "who is the user / how they work" view the vision depends on.
**Done when:** every project's context includes the user profile, regardless of which project.

### ☑ W2-4 · Scope/type nudge on `remember`  *(guardrail)*  → W2-1
**File:** `server.py`.
**How:** heuristic — if `scope=<project>` but statement looks user-level (first-person preference patterns: "I prefer", "user wants", "working style", "always"/"never" about the user), include a `"hint": "looks user-level — consider scope='global'"` in the return. Don't block; nudge.
**Done when:** the consumer's actual mistake (parking user-level facts in project scope) gets surfaced at write time.

---

## Wave 3 — Entities + relationships · the architectural lift  → W2
*Right direction (triple columns reserved; validated by schema research). ⚠ Research also flagged automated KG construction FAILS at identity resolution — build with "propose, don't auto-commit". Never auto-merge "David" and "David Silver".*

### ☐ W3-1 · `entities` table + model
**How:** `entities(id, type, name, scope, created_at, updated_at)` where type ∈ {person, paper, project, file, concept, ...}. Human-readable string ids via `mint_id` (e.g. `ent/person/david`). Identity is by explicit name within type — dedup by name, NOT auto-resolution.
**Done when:** entities can be created and listed; no auto-merge.

### ☐ W3-2 · Fact↔entity edges
**How:** `fact_entities(fact_id, entity_id, role)` — a fact can be "about" multiple entities. Reuse the reserved `facts.subject/predicate/object` columns OR this link table (decide: link table is cleaner for many-to-many). 
**Done when:** a fact attaches to ≥1 entity; query "facts about entity X" works.

### ☐ W3-3 · Parse `[[...]]` into entity links  *(propose, confirm)*
**How:** on `remember`, detect `[[Name]]` / `[[type:Name]]` in statement → propose entity links; create entities if new (cheap), but return them for confirmation rather than silently merging into existing similarly-named entities. Resolves the consumer's inert-syntax confusion.
**Done when:** `[[David]]` in a statement creates/links a person entity; near-duplicate names are surfaced, not auto-merged.

### ☐ W3-4 · Entity-centric query + cross-project rollup
**How:** `get_entity(name_or_id)` → the entity + all facts attached across all projects ("show everything about David", "which projects touch PAC learning"). This is what turns silos into the shared view.
**Done when:** one call returns an entity's full cross-project fact set.

---

## Wave 4 — Semantic / intelligence layer  → W2 (and W3 helps)
*Shared dependency: similarity matching. The remaining guardrails.*

### ☐ W4-1 · Dedup / upsert on `remember`  *(guardrail)*
**How:** before insert, find similar existing facts (start: keyword/`LIKE` + same scope/type; later: embeddings). If a close match exists, return it with an `"existing": [...]` payload and ask whether to update vs create. Enforces the file-protocol's "check for existing first".
**Done when:** near-duplicate facts are surfaced before a second one is written.

### ☐ W4-2 · Conflict detection
**How:** on `remember`, if a new fact about the same subject/entity contradicts a live one, flag it (return both) rather than letting them silently coexist. Leans on W3 entities for "same subject". Pairs naturally with `supersede` (W1-1) as the resolution.
**Done when:** contradictory facts are surfaced, with supersede offered as the fix.

### ☐ W4-3 · Observation→fact digest at `end_session`
**How:** at `end_session`, scan the session's observations and PROPOSE durable facts (return candidates; don't auto-commit — same discipline). Counters the "everything stays an observation" failure mode the consumer + our own docs name.
**Done when:** closing a session offers a short list of promotable facts.

---

## Separate track — v3 Coordination store (already specced)
Multi-agent claims/locks (#12). Not part of this feedback wave — see `SCHEMA.md` §8. Trigger: first time 2+ agents run concurrently on one project.

---

## Suggested order
W1 (all, cheap) → W2-1/W2-2 (typing) → W2-3/W2-4 (profile + nudge) → W3 (entities) → W4 (semantic). Do NOT start W3 before W1 ships — the ergonomics wins are what keep usable feedback flowing, and they're nearly free.
