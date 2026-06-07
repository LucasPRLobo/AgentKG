import uuid
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from mcp.server.fastmcp import FastMCP
from .db import init_db, upsert_node, create_observation, search, get_context
from .models import Node, Observation





mcp = FastMCP("agent-kg")

@mcp.tool()
def ping() -> str:
    """Check if the server is running"""
    return "pong"

if __name__ == "__main__":
    mcp.run()