import uuid
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from mcp.server.fastmcp import FastMCP
from .db import init_db, upsert_node, create_observation, search, get_context, node_exists, mint_id
from .models import Node, Observation


DB_PATH = Path(__file__).parent.parent.parent / "data" / "agent_kg.db"
DB_PATH.parent.mkdir(exist_ok=True)

mcp = FastMCP("agent-kg")
conn: sqlite3.Connection = init_db(DB_PATH)


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
def record_observation(project: str, content: str, session: str | None = None) -> str:
    """
    Record something worth remembering this session: a decision, a constraint,
    a file's purpose. Pass the project name — the server attaches it to that
    project's active session automatically (no session id to track).
    Returns the observation id.
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
    return obs.id


@mcp.tool()
def end_session(project: str, summary: str, session: str | None = None) -> str:
    """
    Close a project's active session with a summary. The summary is what
    future sessions retrieve via get_project_context — make it useful to a
    future agent: what changed, what's in progress, what's next.
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
    return f"Session '{node.label}' closed with summary."


@mcp.tool()
def search_memory(query: str, limit: int = 10) -> list[dict]:
    """
    Search across all sessions and observations using full-text search.
    """
    return search(conn, query, limit)


@mcp.tool()
def get_project_context(project: str) -> dict:
    """
    Load context for a project at the start of a session. Returns recent
    session summaries and observations. Call this before starting work.
    """
    return get_context(conn, project)


if __name__ == "__main__":
    mcp.run()