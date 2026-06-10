import uuid
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from mcp.server.fastmcp import FastMCP
from .db import init_db, upsert_node, create_observation, search, get_context, node_exists
from .models import Node, Observation


DB_PATH = Path(__file__).parent.parent.parent / "data" / "agent_kg.db"
DB_PATH.parent.mkdir(exist_ok=True)

mcp = FastMCP("agent-kg")
conn: sqlite3.Connection = init_db(DB_PATH)


def _now() -> datetime:
    return datetime.now(timezone.utc)

@mcp.tool()
def ping() -> str:
    """Check if the server is running"""
    return "pong"



@mcp.tool()
def start_session(project: str, objective: str) -> str:
    """
    Start a new session for a project. Creates the project node if it 
doesn't
    exist. Returns the session_id to use in subsequent calls.
    """
    now = _now()

    project_node = conn.execute(
        "SELECT id FROM nodes WHERE type = 'project' AND label = ?",
(project,)
    ).fetchone()

    if project_node:
        project_id = project_node["id"]
    else:
        project_id = str(uuid.uuid4())
        upsert_node(conn, Node(
            id=project_id, type="project", label=project,
            created_at=now, updated_at=now
        ))

    session_id = str(uuid.uuid4())
    upsert_node(conn, Node(
        id=session_id, type="session", label=objective,
        project_id=project_id, created_at=now, updated_at=now
    ))

    return session_id


@mcp.tool()
def record_observation(session_id: str, content: str) -> str:
    """
    Record an observation during a session. Call this when you discover
    something worth remembering: a decision, a constraint, a file's purpose.
    Returns the observation_id.
    """
    if not node_exists(conn, session_id, type="session"):
        raise ValueError(
            f"Unknown session_id '{session_id}'. Do not invent session ids — "
            "call start_session first and use the exact id it returns."
        )
    obs = Observation(
        id=str(uuid.uuid4()),
        session_id=session_id,
        content=content,
        created_at=_now(),
    )
    create_observation(conn, obs)
    return obs.id


@mcp.tool()
def end_session(session_id: str, summary: str) -> str:
    """
    Close a session with a summary. The summary is what future sessions
    will retrieve via get_context — make it useful to a future agent.
    """
    now = _now()
    row = conn.execute(
        "SELECT * FROM nodes WHERE id = ? AND type = 'session'", (session_id,)
    ).fetchone()
    if not row:
        raise ValueError(
            f"Unknown session_id '{session_id}'. Do not invent session ids — "
            "use the exact id returned by start_session."
        )
    node = Node(**dict(row))
    node.body = summary
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