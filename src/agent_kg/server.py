import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from mcp.server.fastmcp import FastMCP
from .db import (
    init_db, upsert_node, create_observation, search, get_context, node_exists, mint_id,
    remember as db_remember, recall as db_recall, forget as db_forget,
    supersede as db_supersede, get_fact as db_get_fact, get_observation as db_get_observation,
    list_facts as db_list_facts, list_observations as db_list_observations,
    upsert_entity as db_upsert_entity, find_entity_exact as db_find_entity_exact,
    find_entity_candidates as db_find_entity_candidates, link_fact_entity as db_link_fact_entity,
    list_entities as db_list_entities, get_entity as db_get_entity,
    merge_entities as db_merge_entities, add_alias as db_add_alias,
    find_similar_facts as db_find_similar_facts,
)
from .models import Node, Observation, Fact, Entity


DB_PATH = Path(__file__).parent.parent.parent / "data" / "agent_kg.db"
DB_PATH.parent.mkdir(exist_ok=True)

mcp = FastMCP("agent-kg")
conn: sqlite3.Connection = init_db(DB_PATH)


def _now() -> datetime:
    return datetime.now(timezone.utc)


_USER_LEVEL_HINTS = (
    "i prefer", "i like", "i want", "i don't", "i do not", "i always", "i never",
    "my preference", "my workflow", "user prefers", "user wants", "user likes",
    "working style", "prefers ", "likes to",
)


def _looks_user_level(statement: str) -> bool:
    """Heuristic: does this statement read like a cross-project user preference?"""
    s = statement.lower()
    return any(h in s for h in _USER_LEVEL_HINTS)


_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")


def _parse_wikilinks(statement: str) -> list[tuple[str, str]]:
    """Parse [[Name]] -> (Name, 'concept') and [[type:Name]] -> (Name, type)."""
    out = []
    for m in _WIKILINK.findall(statement):
        if ":" in m:
            t, n = m.split(":", 1)
            out.append((n.strip(), t.strip()))
        else:
            out.append((m.strip(), "concept"))
    return out


def _resolve_session(conn: sqlite3.Connection, project: str, session: str | None = None) -> str:
    """
    Resolve which session a write applies to. Agents pass `project`, NOT a
    session id — the server picks the project's active (most recently updated,
    still-open) session. This makes mistyped/invented session ids impossible.

    If `session` is given explicitly, validate and use it. Raises loudly when
    the id is unknown or the project has no open session.

    Note: two concurrently-open sessions on one project is ambiguous here —
    acceptable for single-agent v2; resolved in v3 via coordination claims.
    """
    if session is not None:
        if not node_exists(conn, session, type="session"):
            raise ValueError(
                f"Unknown session '{session}'. Omit it to use the project's "
                "active session, or call start_session first."
            )
        return session

    row = conn.execute(
        """
        SELECT s.id
        FROM nodes s
        JOIN nodes p ON s.project_id = p.id
        WHERE s.type = 'session' AND s.status = 'open'
          AND p.type = 'project' AND p.label = ?
        ORDER BY s.updated_at DESC
        LIMIT 1
        """,
        (project,),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"No open session for project '{project}'. Call start_session first."
        )
    return row["id"]


@mcp.tool()
def ping() -> str:
    """Check if the server is running"""
    return "pong"



@mcp.tool()
def start_session(project: str, objective: str) -> str:
    """
    Start a new session for a project. Creates the project node if it doesn't
    exist. Returns the session id — but you normally don't need to keep it:
    record_observation and end_session take the project name and use the
    active session automatically.
    """
    now = _now()

    project_node = conn.execute(
        "SELECT id FROM nodes WHERE type = 'project' AND label = ?", (project,)
    ).fetchone()

    if project_node:
        project_id = project_node["id"]
    else:
        project_id = str(uuid.uuid4())
        upsert_node(conn, Node(
            id=project_id, type="project", label=project,
            created_at=now, updated_at=now
        ))

    session_id = mint_id(conn, "s", project)
    upsert_node(conn, Node(
        id=session_id, type="session", label=objective,
        project_id=project_id, status="open", created_at=now, updated_at=now
    ))

    return session_id


@mcp.tool()
def record_observation(project: str, content: str, session: str | None = None) -> dict:
    """
    Record something worth remembering this session: a decision, a constraint,
    a file's purpose. Pass the project name — the server attaches it to that
    project's active session automatically (no session id to track).
    Returns the stored observation (id, scope, session_id, content, timestamp).
    """
    session_id = _resolve_session(conn, project, session)
    obs = Observation(
        id=mint_id(conn, "o", project),
        session_id=session_id,
        content=content,
        scope=project,
        created_at=_now(),
    )
    create_observation(conn, obs)
    return db_get_observation(conn, obs.id)


@mcp.tool()
def end_session(project: str, summary: str, session: str | None = None) -> dict:
    """
    Close a project's active session with a summary. The summary is what future
    sessions retrieve via get_project_context — make it useful to a future agent:
    what changed, what's in progress, what's next. Returns the session's
    observations plus a digest prompt: review them and `remember` any durable
    facts NOW, before they're buried in the raw log.
    """
    session_id = _resolve_session(conn, project, session)
    now = _now()
    row = conn.execute(
        "SELECT * FROM nodes WHERE id = ? AND type = 'session'", (session_id,)
    ).fetchone()
    node = Node(**dict(row))
    node.body = summary
    node.status = "closed"
    node.updated_at = now
    upsert_node(conn, node)

    observations = db_list_observations(conn, session=session_id, limit=100)
    result = {
        "message": f"Session '{node.label}' closed with summary.",
        "session_id": session_id,
        "observations": observations,
    }
    if observations:
        result["digest_hint"] = (
            f"This session recorded {len(observations)} observation(s). Before moving "
            "on, review them and `remember` any DURABLE facts (settled decisions, "
            "constraints, preferences) — observations are the raw log; facts are what "
            "future sessions recall. Don't let durable conclusions stay buried as observations."
        )
    return result


@mcp.tool()
def search_memory(query: str, limit: int = 10) -> list[dict]:
    """
    Search across all sessions and observations using full-text search.
    """
    return search(conn, query, limit)


@mcp.tool()
def get_project_context(project: str) -> dict:
    """
    Load context at the start of a session. Returns recent session summaries,
    recent observations, your global (user-level) facts, and this project's
    facts. Call this before starting work to orient yourself.
    """
    return get_context(conn, project)


@mcp.tool()
def remember(scope: str, statement: str, type: str = "other", confidence: float = 1.0) -> dict:
    """
    Store a durable fact worth keeping across sessions. Use scope='global' for
    user-level knowledge that applies everywhere (preferences, working style), or
    a project name for a fact specific to that project. `type` is one of:
    preference | decision | constraint | reference | person | project | other —
    use 'preference' for user working-style facts (they form the profile shown in
    every project). Returns the stored fact — check the scope/type landed as you
    intended; a `hint` is included if the statement looks misplaced.
    """
    # surface possible duplicates/conflicts BEFORE the new fact exists (W4-1/W4-2)
    similar = db_find_similar_facts(conn, scope, statement)

    fid = db_remember(conn, scope, statement, confidence, type=type)
    result = db_get_fact(conn, fid)
    if scope != "global" and _looks_user_level(statement):
        result["hint"] = (
            "This looks user-level (a preference / working style). If it applies "
            "across all projects, consider scope='global' and type='preference' so "
            "it surfaces everywhere via the profile."
        )
    if similar:
        result["similar"] = [
            {"id": s["id"], "type": s["type"], "statement": s["statement"],
             "overlap": s["overlap"], "effective_confidence": s["effective_confidence"]}
            for s in similar
        ]
        result["similar_hint"] = (
            f"{len(similar)} existing fact(s) in scope '{scope}' overlap this one. "
            "If this UPDATES/REPLACES one, call supersede(old_fact_id, ...) instead of "
            "keeping both; if it's a DUPLICATE, forget the extra. Otherwise ignore."
        )
    # [[Name]] / [[type:Name]] in the statement auto-link the fact to entities
    links = _parse_wikilinks(statement)
    if links:
        linked = []
        for nm, ty in links:
            ent = create_entity(nm, ty)
            db_link_fact_entity(conn, fid, ent["id"], "about")
            linked.append({"name": nm, "type": ty, "entity_id": ent["id"],
                           "matched": ent.get("matched"), "candidates": ent.get("candidates")})
        result["entity_links"] = linked
    return result


@mcp.tool()
def recall(query: str, scope: str | None = None, as_of: str | None = None,
           limit: int = 10, type: str | None = None) -> list[dict]:
    """
    Search durable facts (not raw observations), ranked by decayed confidence.
    Scope modes: omit scope for global (user-level) facts only; pass a project
    name for that project's facts PLUS your global facts; pass scope='all' to
    search across every project. Pass `type` to filter (preference | decision |
    constraint | reference | person | project | other). Pass as_of (an ISO-8601
    timestamp) to see what was believed at a past time. Each result includes
    effective_confidence and age_days so you can judge staleness.
    """
    parsed = None
    if as_of is not None:
        try:
            parsed = datetime.fromisoformat(as_of)
        except ValueError:
            raise ValueError(
                f"as_of must be an ISO-8601 timestamp "
                f"(e.g. 2026-06-11T00:00:00+00:00), got '{as_of}'."
            )
    return db_recall(conn, query, scope=scope, as_of=parsed, limit=limit, type=type)


@mcp.tool()
def supersede(old_fact_id: str, new_statement: str, scope: str | None = None,
              confidence: float = 1.0) -> dict:
    """
    Replace a fact whose content has changed. Retires old_fact_id (soft-invalidated,
    preserved for point-in-time history) and creates a new fact structurally linked
    back to it via `supersedes`. Use this instead of forget+remember when a decision
    or value evolved — it keeps the supersession chain queryable. Scope defaults to
    the old fact's scope if omitted. Returns the new fact.
    """
    old = conn.execute("SELECT * FROM facts WHERE id = ?", (old_fact_id,)).fetchone()
    if old is None:
        raise ValueError(f"Unknown fact id '{old_fact_id}'. Use recall/list_facts to find it.")
    now = _now()
    new_fact = Fact(
        id=mint_id(conn, "f", "global"),
        scope=scope if scope is not None else old["scope"],
        statement=new_statement,
        type=old["type"],
        confidence=confidence,
        t_created=now, last_confirmed_at=now, created_at=now, updated_at=now,
    )
    db_supersede(conn, old_fact_id, new_fact)
    return db_get_fact(conn, new_fact.id)


@mcp.tool()
def forget(fact_id: str) -> dict:
    """
    Soft-invalidate a fact that is no longer true. It stops appearing in recall
    going forward but is preserved for point-in-time history. Returns the
    invalidated fact (now carrying t_invalid).
    """
    if not conn.execute("SELECT 1 FROM facts WHERE id = ?", (fact_id,)).fetchone():
        raise ValueError(f"Unknown fact id '{fact_id}'. Use recall to find the right id.")
    db_forget(conn, fact_id)
    return db_get_fact(conn, fact_id)


@mcp.tool()
def list_facts(scope: str | None = None, include_invalid: bool = False, limit: int = 50,
               type: str | None = None) -> list[dict]:
    """
    Browse stored facts (enumerate, not search). Omit scope to list every scope;
    pass a project name or 'global' to filter. Pass `type` to filter by category
    (preference | decision | constraint | reference | person | project | other).
    Set include_invalid=True to also show forgotten/superseded facts. Ordered
    newest first; each carries effective_confidence and age_days.
    """
    return db_list_facts(conn, scope=scope, include_invalid=include_invalid, limit=limit, type=type)


@mcp.tool()
def list_observations(project: str | None = None, session: str | None = None, limit: int = 50) -> list[dict]:
    """
    Browse raw observations (enumerate, not search). Filter by project name and/or
    a specific session id. Ordered newest first.
    """
    return db_list_observations(conn, project=project, session=session, limit=limit)


@mcp.tool()
def create_entity(name: str, type: str, scope: str = "global") -> dict:
    """
    Create or fetch a shared entity (person | paper | project | file | concept | other),
    global by default so it's the same node across all projects. An exact (name, type)
    match returns the existing entity. If a NEW entity is created but similarly-named
    ones already exist, they are returned under `candidates` with a `hint` — entities
    are NEVER auto-merged across different names; confirm with merge_entities yourself.
    """
    exact = db_find_entity_exact(conn, name, type)
    if exact:
        return {**exact, "matched": "exact"}
    now = _now()
    ent = Entity(id=mint_id(conn, "e", "global"), type=type, name=name, scope=scope,
                 created_at=now, updated_at=now)
    db_upsert_entity(conn, ent)
    out = {**ent.model_dump(mode="json"), "matched": "created"}
    candidates = db_find_entity_candidates(conn, name, type)
    if candidates:
        out["candidates"] = candidates
        out["hint"] = (
            f"Created a new {type} '{name}', but {len(candidates)} similarly-named "
            f"{type}(s) already exist. If this is the same entity, call merge_entities — "
            "agent-kg never auto-merges across different names."
        )
    return out


@mcp.tool()
def link_fact(fact_id: str, name: str, type: str, role: str = "about", scope: str = "global") -> dict:
    """
    Attach a fact to an entity, creating or finding the entity by (name, type). `role`
    describes the relationship (about | mentions | authored_by | depends_on | ...).
    Returns the link and the resolved entity; if a new entity was created alongside
    similarly-named existing ones, they're surfaced as candidates (never auto-merged).
    """
    if not conn.execute("SELECT 1 FROM facts WHERE id = ?", (fact_id,)).fetchone():
        raise ValueError(f"Unknown fact id '{fact_id}'. Use recall/list_facts to find it.")
    entity = create_entity(name, type, scope)
    db_link_fact_entity(conn, fact_id, entity["id"], role)
    return {"fact_id": fact_id, "entity_id": entity["id"], "role": role, "entity": entity}


@mcp.tool()
def list_entities(type: str | None = None, scope: str | None = None, limit: int = 50) -> list[dict]:
    """
    Browse stored entities (enumerate, not search). Optionally filter by type
    (person | paper | project | file | concept | other) and/or scope. Ordered newest first.
    """
    return db_list_entities(conn, type=type, scope=scope, limit=limit)


@mcp.tool()
def get_entity(name_or_id: str) -> dict:
    """
    Look up an entity by name, alias, or id and return EVERYTHING known about it:
    the entity, its aliases, and all live facts linked to it ACROSS EVERY PROJECT,
    plus facts_by_scope (which projects touch it). Follows merge redirects, so a
    merged-away id still resolves. This is the cross-project shared view.
    """
    result = db_get_entity(conn, name_or_id)
    if result is None:
        raise ValueError(f"No entity found for '{name_or_id}'. Use list_entities to browse.")
    return result


@mcp.tool()
def merge_entities(from_id: str, to_id: str) -> dict:
    """
    Merge two entities that are the SAME thing. Redirects from_id -> to_id
    (non-destructive: the merged node is kept and invalidated, its name preserved as
    an alias, fact links resolve through the redirect). Reversible. Call this
    deliberately to confirm a duplicate surfaced as a candidate — agent-kg never
    auto-merges. Returns the surviving entity's full view.
    """
    for eid in (from_id, to_id):
        if not conn.execute("SELECT 1 FROM entities WHERE id = ?", (eid,)).fetchone():
            raise ValueError(f"Unknown entity id '{eid}'. Use list_entities to find it.")
    db_merge_entities(conn, from_id, to_id)
    return db_get_entity(conn, to_id)


@mcp.tool()
def add_alias(entity_id: str, alias: str) -> dict:
    """
    Add an alternate name (sameAs / aka) to an entity so future lookups by that name
    resolve to it. Returns the entity's full view.
    """
    if not conn.execute("SELECT 1 FROM entities WHERE id = ?", (entity_id,)).fetchone():
        raise ValueError(f"Unknown entity id '{entity_id}'. Use list_entities to find it.")
    db_add_alias(conn, entity_id, alias)
    return db_get_entity(conn, entity_id)


if __name__ == "__main__":
    mcp.run()