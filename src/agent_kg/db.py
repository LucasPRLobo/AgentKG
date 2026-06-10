import sqlite3
from pathlib import Path
from .models import Node, Observation


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
        INSERT INTO nodes (id, type, label, body, project_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            label      = excluded.label,
            body       = excluded.body,
            updated_at = excluded.updated_at
        """,
        (node.id, node.type, node.label, node.body, node.project_id,
         node.created_at.isoformat(), node.updated_at.isoformat())
    )
    conn.commit()


def create_observation(conn: sqlite3.Connection, obs: Observation) -> None:
    conn.execute(
        "INSERT INTO observations (id, session_id, content, created_at) VALUES (?, ?, ?, ?)",
        (obs.id, obs.session_id, obs.content, obs.created_at.isoformat())
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


def get_context(conn: sqlite3.Connection, project_label: str, session_limit: int = 5, obs_limit: int = 20) -> dict:
    project = conn.execute(
        "SELECT * FROM nodes WHERE type = 'project' AND label = ?",
        (project_label,)
    ).fetchone()

    if not project:
        return {"project": None, "sessions": []}

    sessions = conn.execute(
        """
        SELECT s.id, s.label, s.body, s.created_at, s.updated_at,
               count(o.id) AS obs_count,
               (s.body IS NULL) AS dangling
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
        "sessions": [dict(s) for s in sessions],
        "observations": [dict(o) for o in observations],
    }
