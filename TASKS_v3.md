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

## Wave 3 — Entities + relationships · the architectural lift ✅ COMPLETE (verified on real-DB backup 2026-06-18)
**Design SETTLED by `entity_schema_research` (2026-06-18):** Option B — facts stay first-class statements; entities attach via a many-to-many **`fact_entities` link table** (Hogan two-table pattern), NOT the reserved s/p/o columns. Entities are **global-scoped by default** (one shared node across projects → cross-project rollup). Identity resolution is **propose-don't-auto-commit**: exact name+type → auto-link; cross-name/fuzzy → surface as proposal, never auto-merge. Merges are **non-destructive redirects** (reversible). Thin first cut: **manual linking + exact-match auto; embedding-based candidate generation DEFERRED** to a later wave.
⚠ Confidence gate alone is insufficient (research: "confidently-wrong" links) — keep every merge reversible.

### ☑ W3-1 · Entity schema + model  *(migration)*
**File:** `db.py` (executescript + migration loop) + `models.py`.
**How:** four additive tables (idempotent, same pattern as prior migrations):
- `entities(id, type, name, scope, created_at, updated_at, t_invalid)` — `scope` defaults `'global'`; `type` ∈ person|paper|project|file|concept|other; id via `mint_id` (kind `e`, scope `global` → `global/e1`, or slug `ent/<type>/<name>`).
- `entity_aliases(entity_id, alias, PRIMARY KEY(entity_id, alias))` — sameAs/akas.
- `entity_redirects(from_id, to_id, created_at)` — non-destructive merge.
- `fact_entities(fact_id, entity_id, role, PRIMARY KEY(fact_id, entity_id, role))` — the link table.
- `Entity` pydantic model.
**Done when:** migration runs idempotently on a real-DB backup, data intact; tables present; `Entity(**row)` round-trips.

### ☑ W3-2 · Entity create + fact linking (manual)  → W3-1
**File:** `db.py` + `server.py`.
**How:**
- `db.upsert_entity` / `db.link_fact_entity(fact_id, entity_id, role)` / `db.list_entities(type=None, scope=None)`.
- Tools: `create_entity(name, type, scope='global')` — **exact (name,type) match → return existing (auto)**; `link_fact(fact_id, entity_name_or_id, role='about')` — resolve/create then link; `list_entities(...)`.
- Loud validation: unknown fact_id raises (reuse the pattern).
**Done when:** a fact links to ≥1 entity; exact-name re-create returns the same node (no dup); linking unknown fact fails loudly.

### ☑ W3-3 · Propose-don't-auto-commit resolution  → W3-2
**File:** `db.py` + `server.py`.
**How:** on `create_entity`/`link_fact`, after the exact-match check, run a **candidate scan** (FTS/`LIKE` over names + aliases, same type) → if near-matches exist but no exact, **return `{"created": <new>, "candidates": [...], "hint": "possible duplicates — confirm or merge_entities"}`** rather than silently merging. Never auto-merge cross-name. (Embedding similarity deferred.)
**Done when:** creating "David Silver" when "David" exists creates the new one BUT surfaces "David" as a candidate; exact "David" reuses silently.

### ☑ W3-4 · Entity query + cross-project rollup + non-destructive merge  → W3-2
**File:** `db.py` + `server.py`.
**How:**
- `get_entity(name_or_id)` → entity (following redirects) + ALL linked facts across every project, grouped by fact scope ("show everything about David" / "which projects touch PAC"). 
- `merge_entities(from_id, to_id)` → write an `entity_redirects` row, re-point `fact_entities` from→to, set `from` entity `t_invalid` (kept, not deleted). Reversible. Confirmation-gated (the agent calls it deliberately).
- `add_alias(entity_id, alias)`.
**Done when:** get_entity returns cross-project facts; merge redirects + re-points non-destructively; a redirected id still resolves via get_entity.

### ☑ W3-5 · `[[...]]` auto-link in remember  *(optional convenience)*  → W3-2/W3-3
**How:** detect `[[Name]]` / `[[type:Name]]` in a remembered statement → propose entity links via the W3-3 path (create-or-link, surface candidates). Resolves the consumer's inert-syntax confusion. Lowest priority in the wave; can ship after W3-4.
**Done when:** `[[David]]` in a statement links the fact to a David entity (exact) or surfaces candidates (fuzzy).

---

## Wave 4 — Semantic / intelligence layer ✅ COMPLETE (verified on real-DB backup 2026-06-18)
*Thin cut: token-overlap similarity (embeddings deferred); W4-2 surfaces possible conflicts via `similar` + offers supersede — true semantic contradiction detection deferred. W4-3 digest = server returns observations + nudge; the agent (the intelligence) promotes.*
*Shared dependency: similarity matching. The remaining guardrails.*

### ☑ W4-1 · Dedup / upsert on `remember`  *(guardrail)*
**How:** before insert, find similar existing facts (start: keyword/`LIKE` + same scope/type; later: embeddings). If a close match exists, return it with an `"existing": [...]` payload and ask whether to update vs create. Enforces the file-protocol's "check for existing first".
**Done when:** near-duplicate facts are surfaced before a second one is written.

### ☑ W4-2 · Conflict detection
**How:** on `remember`, if a new fact about the same subject/entity contradicts a live one, flag it (return both) rather than letting them silently coexist. Leans on W3 entities for "same subject". Pairs naturally with `supersede` (W1-1) as the resolution.
**Done when:** contradictory facts are surfaced, with supersede offered as the fix.

### ☑ W4-3 · Observation→fact digest at `end_session`
**How:** at `end_session`, scan the session's observations and PROPOSE durable facts (return candidates; don't auto-commit — same discipline). Counters the "everything stays an observation" failure mode the consumer + our own docs name.
**Done when:** closing a session offers a short list of promotable facts.

---

## Separate track — v3 Coordination store (already specced)
Multi-agent claims/locks (#12). Not part of this feedback wave — see `SCHEMA.md` §8. Trigger: first time 2+ agents run concurrently on one project.

---

## Suggested order
W1 (all, cheap) → W2-1/W2-2 (typing) → W2-3/W2-4 (profile + nudge) → W3 (entities) → W4 (semantic). Do NOT start W3 before W1 ships — the ergonomics wins are what keep usable feedback flowing, and they're nearly free.
