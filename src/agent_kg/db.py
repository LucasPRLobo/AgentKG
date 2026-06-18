import sqlite3
import math
from datetime import datetime, timezone
from pathlib import Path
from .models import Node, Observation, Fact, Entity


LAMBDA = math.log(2) / 90  # 90-day half-life

def init_db(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            id         TEXT PRIMARY KEY,
            type       TEXT NOT NULL,
            label      TEXT NOT NULL,
            body       TEXT,
            project_id TEXT REFERENCES nodes(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS observations (
            id         TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES nodes(id),
            content    TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS facts (
            id         TEXT PRIMARY KEY,
            scope      TEXT NOT NULL,
            statement  TEXT NOT NULL,
            type       TEXT NOT NULL DEFAULT 'other',

            subject     TEXT,
            predicate   TEXT,
            object      TEXT,

            t_created   TEXT NOT NULL,
            t_invalid   TEXT,
            valid_from  TEXT,
            valid_until TEXT,

            confidence  REAL NOT NULL DEFAULT 1.0,
            recurrence_count INTEGER NOT NULL DEFAULT 1,
            last_confirmed_at TEXT NOT NULL,

            supersedes      TEXT REFERENCES facts(id),
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS fact_sources (
            fact_id        TEXT NOT NULL REFERENCES facts(id),
            observation_id TEXT NOT NULL REFERENCES observations(id),
            PRIMARY KEY (fact_id, observation_id)
        );

        CREATE TABLE IF NOT EXISTS counters (
            scope TEXT NOT NULL,
            kind  TEXT NOT NULL,
            n     INTEGER NOT NULL,
            PRIMARY KEY (scope, kind)
        );

        CREATE TABLE IF NOT EXISTS entities (
            id         TEXT PRIMARY KEY,
            type       TEXT NOT NULL,
            name       TEXT NOT NULL,
            scope      TEXT NOT NULL DEFAULT 'global',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            t_invalid  TEXT
        );

        CREATE TABLE IF NOT EXISTS entity_aliases (
            entity_id TEXT NOT NULL REFERENCES entities(id),
            alias     TEXT NOT NULL,
            PRIMARY KEY (entity_id, alias)
        );

        CREATE TABLE IF NOT EXISTS entity_redirects (
            from_id    TEXT PRIMARY KEY REFERENCES entities(id),
            to_id      TEXT NOT NULL REFERENCES entities(id),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS fact_entities (
            fact_id   TEXT NOT NULL REFERENCES facts(id),
            entity_id TEXT NOT NULL REFERENCES entities(id),
            role      TEXT NOT NULL DEFAULT 'about',
            PRIMARY KEY (fact_id, entity_id, role)
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS obs_fts USING fts5(
            content,
            content=observations,
            content_rowid=rowid
        );
      """)

    # Migration: add new columns to existing tables (idempotent)
    for table, col, decl in [
        ("observations", "scope",       "TEXT"),
        ("observations", "agent_id",    "TEXT"),
        ("observations", "promoted_to", "TEXT"),
        ("nodes",        "status",      "TEXT"),
        ("facts",        "type",        "TEXT NOT NULL DEFAULT 'other'"),
    ]:
        if not _has_column(conn, table, col):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    _backfill(conn)
    conn.commit()
    return conn


def _has_column(conn, table, col):
    return any(r["name"] == col for r in conn.execute(f"SELECT name FROM pragma_table_info('{table}')"))


def _backfill(conn: sqlite3.Connection) -> None:
    # scope <- project label reached via session -> project
    conn.execute("""
        UPDATE observations
        SET scope = (
            SELECT p.label
            FROM nodes s
            JOIN nodes p ON s.project_id = p.id
            WHERE s.id = observations.session_id
        )
        WHERE scope IS NULL
    """)
    # status <- 'open' if never closed (no summary), else 'closed'
    conn.execute("""
        UPDATE nodes
        SET status = CASE WHEN body IS NULL THEN 'open' ELSE 'closed' END
        WHERE type = 'session' AND status IS NULL
    """)


def mint_id(conn: sqlite3.Connection, kind: str, scope: str) -> str:
    # kind in {'o','s','f'}; fact counter is global, so scope='global' for facts
    conn.execute(
        "INSERT INTO counters (scope, kind, n) VALUES (?, ?, 1) "
        "ON CONFLICT(scope, kind) DO UPDATE SET n = n + 1",
        (scope, kind),
    )
    n = conn.execute(
        "SELECT n FROM counters WHERE scope = ? AND kind = ?", (scope, kind)
    ).fetchone()["n"]
    if kind == "f":
        return f"f{n}"
    return f"{scope}/{kind}{n}"


def node_exists(conn: sqlite3.Connection, node_id: str, type: str | None = None) -> bool:
    if type is None:
        row = conn.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone()
    else:
        row = conn.execute("SELECT 1 FROM nodes WHERE id = ? AND type = ?", (node_id, type)).fetchone()
    return row is not None


def upsert_node(conn: sqlite3.Connection, node: Node) -> None:
    conn.execute(
        """
        INSERT INTO nodes (id, type, label, body, project_id, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            label      = excluded.label,
            body       = excluded.body,
            status     = excluded.status,
            updated_at = excluded.updated_at
        """,
        (node.id, node.type, node.label, node.body, node.project_id, node.status,
         node.created_at.isoformat(), node.updated_at.isoformat())
    )
    conn.commit()


def create_observation(conn: sqlite3.Connection, obs: Observation) -> None:
    conn.execute(
        "INSERT INTO observations (id, session_id, content, scope, agent_id, promoted_to, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (obs.id, obs.session_id, obs.content, obs.scope, obs.agent_id, obs.promoted_to,
         obs.created_at.isoformat())
    )
    conn.execute(
        """
        INSERT INTO obs_fts (rowid, content)
        VALUES ((SELECT rowid FROM observations WHERE id = ?), ?)
        """,
        (obs.id, obs.content)
    )
    conn.commit()


def search(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    obs_rows = conn.execute(
        """
        SELECT 'observation' AS kind, o.id, o.session_id, o.content, o.created_at
        FROM obs_fts
        JOIN observations o ON obs_fts.rowid = o.rowid
        WHERE obs_fts MATCH ?
        LIMIT ?
        """,
        (query, limit)
    ).fetchall()

    node_rows = conn.execute(
        """
        SELECT 'node' AS kind, id, type, label, body, created_at
        FROM nodes
        WHERE label LIKE ? OR body LIKE ?
        LIMIT ?
        """,
        (f"%{query}%", f"%{query}%", limit)
    ).fetchall()

    return [dict(r) for r in obs_rows] + [dict(r) for r in node_rows]


def _rank_live_facts(conn: sqlite3.Connection, scope_exact: str, limit: int, now: datetime) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM facts WHERE scope = ? AND t_invalid IS NULL",
        (scope_exact,),
    ).fetchall()
    out = [_enrich_fact(dict(row), now) for row in rows]
    out.sort(key=lambda d: d["effective_confidence"], reverse=True)
    return out[:limit]


def get_context(conn: sqlite3.Connection, project_label: str, session_limit: int = 5,
                obs_limit: int = 20, fact_limit: int = 10) -> dict:
    now = datetime.now(timezone.utc)
    # facts are scoped by label, independent of whether a project node exists
    global_facts = _rank_live_facts(conn, "global", fact_limit, now)
    project_facts = _rank_live_facts(conn, project_label, fact_limit, now)
    # the user profile: global preference facts ("who is the user / how they work"),
    # surfaced in EVERY project regardless of which one you're in
    profile = list_facts(conn, scope="global", type="preference", limit=fact_limit)

    project = conn.execute(
        "SELECT * FROM nodes WHERE type = 'project' AND label = ?",
        (project_label,)
    ).fetchone()

    if not project:
        return {
            "project": None,
            "status": "new_project_no_prior_context",
            "profile": profile,
            "sessions": [],
            "observations": [],
            "global_facts": global_facts,
            "project_facts": project_facts,
        }

    sessions = conn.execute(
        """
        SELECT s.id, s.label, s.body, s.status, s.created_at, s.updated_at,
               count(o.id) AS obs_count,
               (s.status = 'open') AS dangling
        FROM nodes s
        LEFT JOIN observations o ON o.session_id = s.id
        WHERE s.type = 'session' AND s.project_id = ?
        GROUP BY s.id
        ORDER BY s.updated_at DESC
        LIMIT ?
        """,
        (project["id"], session_limit)
    ).fetchall()

    observations = conn.execute(
        """
        SELECT o.id, o.session_id, o.content, o.created_at
        FROM observations o
        JOIN nodes s ON o.session_id = s.id
        WHERE s.project_id = ?
        ORDER BY o.created_at DESC
        LIMIT ?
        """,
        (project["id"], obs_limit)
    ).fetchall()

    return {
        "project": dict(project),
        "profile": profile,
        "sessions": [dict(s) for s in sessions],
        "observations": [dict(o) for o in observations],
        "global_facts": global_facts,
        "project_facts": project_facts,
    }

def create_fact(conn: sqlite3.Connection, fact: Fact, source_obs_ids: list[str]) -> None:
    conn.execute(
        """
        INSERT INTO facts (id, scope, statement, type, subject, predicate, object,
                            t_created, t_invalid, valid_from, valid_until,
                            confidence, recurrence_count, last_confirmed_at,
                            supersedes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (fact.id, fact.scope, fact.statement, fact.type, fact.subject, fact.predicate, fact.object,
        fact.t_created.isoformat(),
        fact.t_invalid.isoformat() if fact.t_invalid else None,
        fact.valid_from.isoformat() if fact.valid_from else None,
        fact.valid_until.isoformat() if fact.valid_until else None,
        fact.confidence, fact.recurrence_count, fact.last_confirmed_at.isoformat(),
        fact.supersedes, fact.created_at.isoformat(), fact.updated_at.isoformat()),
    )
    for obs_id in source_obs_ids:
        conn.execute(
            "INSERT INTO fact_sources (fact_id, observation_id) VALUES (?, ?)",
            (fact.id, obs_id),
        )
        conn.execute(
            "UPDATE observations SET promoted_to = ? WHERE id = ?",
            (fact.id, obs_id),
        )
    conn.commit()



def effective_confidence(base: float, last_confirmed_at: datetime, now: datetime) -> float:
    age_days = (now - last_confirmed_at).total_seconds() / 86400
    return base * math.exp(-LAMBDA * age_days)


def recall(conn: sqlite3.Connection, query: str, scope: str | None = None,
           as_of: datetime | None = None, limit: int = 10, type: str | None = None) -> list[dict]:
    as_of = as_of or datetime.now(timezone.utc)
    as_of_iso = as_of.isoformat()

    sql = """
        SELECT * FROM facts
        WHERE statement LIKE ?
          AND t_created <= ?
          AND (t_invalid IS NULL OR t_invalid > ?)
    """
    params: list = [f"%{query}%", as_of_iso, as_of_iso]
    # scope modes: None -> global only; "<project>" -> project + global; "all" -> everything
    if scope == "all":
        pass
    elif scope is None:
        sql += " AND scope = 'global'"
    else:
        sql += " AND scope IN (?, 'global')"
        params.append(scope)

    if type is not None:
        sql += " AND type = ?"
        params.append(type)

    rows = conn.execute(sql, params).fetchall()
    results = [_enrich_fact(dict(row), as_of) for row in rows]
    results.sort(key=lambda d: d["effective_confidence"], reverse=True)
    return results[:limit]

def remember(conn, scope, statement, confidence=1.0, type="other", source_obs_ids=()):
    now = datetime.now(timezone.utc)
    fid = mint_id(conn, "f", "global")
    fact = Fact(id=fid, scope=scope, statement=statement, type=type, confidence=confidence,
                t_created=now, last_confirmed_at=now, created_at=now, updated_at=now)
    create_fact(conn, fact, list(source_obs_ids))
    return fid

def forget(conn, fact_id):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE facts SET t_invalid = ? WHERE id = ?", (now, fact_id))
    conn.commit()

def supersede(conn, old_id, new_fact):
    conn.execute("UPDATE facts SET t_invalid = ? WHERE id = ?",
                (new_fact.t_created.isoformat(), old_id))
    new_fact.supersedes = old_id
    create_fact(conn, new_fact, [])
    conn.commit()


def _enrich_fact(d: dict, as_of: datetime) -> dict:
    """Add agent-facing derived fields to a raw fact row: decayed confidence + age."""
    last = datetime.fromisoformat(d["last_confirmed_at"])
    d["effective_confidence"] = effective_confidence(d["confidence"], last, as_of)
    d["age_days"] = (as_of - last).total_seconds() / 86400
    return d


def get_fact(conn: sqlite3.Connection, fact_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    if row is None:
        return None
    return _enrich_fact(dict(row), datetime.now(timezone.utc))


def get_observation(conn: sqlite3.Connection, obs_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
    return dict(row) if row else None


def list_facts(conn: sqlite3.Connection, scope: str | None = None,
               include_invalid: bool = False, limit: int = 50, type: str | None = None) -> list[dict]:
    now = datetime.now(timezone.utc)
    clauses, params = [], []
    if not include_invalid:
        clauses.append("t_invalid IS NULL")
    if scope is not None and scope != "all":
        clauses.append("scope = ?")
        params.append(scope)
    if type is not None:
        clauses.append("type = ?")
        params.append(type)
    sql = "SELECT * FROM facts"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY t_created DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_enrich_fact(dict(r), now) for r in rows]


def list_observations(conn: sqlite3.Connection, project: str | None = None,
                      session: str | None = None, limit: int = 50) -> list[dict]:
    clauses, params = [], []
    if session is not None:
        clauses.append("session_id = ?")
        params.append(session)
    if project is not None:
        clauses.append("scope = ?")
        params.append(project)
    sql = "SELECT * FROM observations"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---- entities (Wave 3) ----

def upsert_entity(conn: sqlite3.Connection, entity: Entity) -> None:
    conn.execute(
        """
        INSERT INTO entities (id, type, name, scope, created_at, updated_at, t_invalid)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name       = excluded.name,
            type       = excluded.type,
            scope      = excluded.scope,
            updated_at = excluded.updated_at,
            t_invalid  = excluded.t_invalid
        """,
        (entity.id, entity.type, entity.name, entity.scope,
         entity.created_at.isoformat(), entity.updated_at.isoformat(),
         entity.t_invalid.isoformat() if entity.t_invalid else None),
    )
    conn.commit()


def find_entity_exact(conn: sqlite3.Connection, name: str, type: str) -> dict | None:
    """Exact identity: same (name, type) among live entities, or a matching alias."""
    row = conn.execute(
        "SELECT * FROM entities WHERE name = ? AND type = ? AND t_invalid IS NULL",
        (name, type),
    ).fetchone()
    if row:
        return dict(row)
    row = conn.execute(
        """
        SELECT e.* FROM entities e
        JOIN entity_aliases a ON a.entity_id = e.id
        WHERE a.alias = ? AND e.type = ? AND e.t_invalid IS NULL
        """,
        (name, type),
    ).fetchone()
    return dict(row) if row else None


def find_entity_candidates(conn: sqlite3.Connection, name: str, type: str, limit: int = 5) -> list[dict]:
    """Fuzzy near-matches (substring either direction), same type, live, excluding exact.
    Crude on purpose — embedding-based candidate generation is deferred."""
    rows = conn.execute(
        """
        SELECT * FROM entities
        WHERE type = ? AND t_invalid IS NULL AND name <> ?
          AND (name LIKE ? OR ? LIKE '%' || name || '%')
        LIMIT ?
        """,
        (type, name, f"%{name}%", name, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def link_fact_entity(conn: sqlite3.Connection, fact_id: str, entity_id: str, role: str = "about") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO fact_entities (fact_id, entity_id, role) VALUES (?, ?, ?)",
        (fact_id, entity_id, role),
    )
    conn.commit()


def list_entities(conn: sqlite3.Connection, type: str | None = None,
                  scope: str | None = None, limit: int = 50) -> list[dict]:
    clauses, params = ["t_invalid IS NULL"], []
    if type is not None:
        clauses.append("type = ?")
        params.append(type)
    if scope is not None:
        clauses.append("scope = ?")
        params.append(scope)
    sql = "SELECT * FROM entities WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def resolve_entity_id(conn: sqlite3.Connection, entity_id: str) -> str:
    """Follow merge redirects (transitively) to the surviving entity id."""
    seen = set()
    while entity_id not in seen:
        seen.add(entity_id)
        row = conn.execute("SELECT to_id FROM entity_redirects WHERE from_id = ?", (entity_id,)).fetchone()
        if not row:
            return entity_id
        entity_id = row["to_id"]
    return entity_id  # cycle guard


def _redirect_closure(conn: sqlite3.Connection, target_id: str) -> set:
    """All entity ids that resolve to target_id (target + everything merged into it)."""
    ids = {target_id}
    changed = True
    while changed:
        changed = False
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT from_id FROM entity_redirects WHERE to_id IN ({placeholders})", tuple(ids)
        ).fetchall()
        for r in rows:
            if r["from_id"] not in ids:
                ids.add(r["from_id"])
                changed = True
    return ids


def add_alias(conn: sqlite3.Connection, entity_id: str, alias: str) -> None:
    conn.execute("INSERT OR IGNORE INTO entity_aliases (entity_id, alias) VALUES (?, ?)", (entity_id, alias))
    conn.commit()


def merge_entities(conn: sqlite3.Connection, from_id: str, to_id: str) -> None:
    """Non-destructive merge: redirect from_id -> surviving to_id, keep the merged
    name as an alias, invalidate the merged node. Fact links are NOT moved — queries
    resolve through the redirect closure — so the merge is fully reversible (drop the
    redirect row + clear t_invalid)."""
    to_final = resolve_entity_id(conn, to_id)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO entity_redirects (from_id, to_id, created_at) VALUES (?, ?, ?)",
        (from_id, to_final, now),
    )
    frow = conn.execute("SELECT name FROM entities WHERE id = ?", (from_id,)).fetchone()
    if frow:
        conn.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
            (to_final, frow["name"]),
        )
    conn.execute("UPDATE entities SET t_invalid = ?, updated_at = ? WHERE id = ?", (now, now, from_id))
    conn.commit()


def get_entity(conn: sqlite3.Connection, name_or_id: str) -> dict | None:
    """Resolve an entity (by id following redirects, or by name/alias) and return it
    with ALL live facts linked to it across every project, grouped by scope."""
    row = conn.execute("SELECT 1 FROM entities WHERE id = ?", (name_or_id,)).fetchone()
    if row:
        target = resolve_entity_id(conn, name_or_id)
    else:
        r = conn.execute(
            "SELECT id FROM entities WHERE name = ? AND t_invalid IS NULL LIMIT 1", (name_or_id,)
        ).fetchone()
        if not r:
            r = conn.execute(
                "SELECT entity_id AS id FROM entity_aliases WHERE alias = ? LIMIT 1", (name_or_id,)
            ).fetchone()
        if not r:
            return None
        target = resolve_entity_id(conn, r["id"])

    ent = dict(conn.execute("SELECT * FROM entities WHERE id = ?", (target,)).fetchone())
    ids = _redirect_closure(conn, target)
    placeholders = ",".join("?" * len(ids))
    frows = conn.execute(
        f"""
        SELECT DISTINCT f.* FROM facts f
        JOIN fact_entities fe ON fe.fact_id = f.id
        WHERE fe.entity_id IN ({placeholders}) AND f.t_invalid IS NULL
        """,
        tuple(ids),
    ).fetchall()
    now = datetime.now(timezone.utc)
    facts = [_enrich_fact(dict(r), now) for r in frows]
    by_scope: dict = {}
    for fct in facts:
        by_scope.setdefault(fct["scope"], []).append(fct["id"])
    aliases = [r["alias"] for r in conn.execute(
        "SELECT alias FROM entity_aliases WHERE entity_id = ?", (target,)).fetchall()]
    return {
        "entity": ent,
        "aliases": aliases,
        "facts": facts,
        "facts_by_scope": by_scope,
        "resolved_id": target,
    }