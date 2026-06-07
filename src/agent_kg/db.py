import sqlite3
from pathlib import Path
from .models import Node, Observation

def init_db(path: str | Path) -> sqlite3.Connection:
    conn  = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
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

          CREATE VIRTUAL TABLE IF NOT EXISTS obs_fts USING fts5(
              content,
              content=observations,
              content_rowid=rowid
          );
      """)
    conn.commit()
    return conn


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


def get_context(conn: sqlite3.Connection, project_label: str, session_limit:
int = 5) -> dict:
    project = conn.execute(
        "SELECT * FROM nodes WHERE type = 'project' AND label = ?",
        (project_label,)
    ).fetchone()

    if not project:
        return {"project": None, "sessions": []}

    sessions = conn.execute(
        """
        SELECT id, label, body, created_at, updated_at
        FROM nodes
        WHERE type = 'session' AND project_id = ?
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (project["id"], session_limit)
    ).fetchall()

    return {
        "project": dict(project),
        "sessions": [dict(s) for s in sessions],
    }