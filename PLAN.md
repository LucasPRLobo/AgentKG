# agent-kg v1: Implementation Plan

## Goal

A system that lets a Claude Code session record what it did and load prior context at the start of future sessions. Nothing more.

**Not in v1:** LLM extraction, graph traversal, relationship edges, multi-key indexing, promotion rules. Those come after we've used the thing for real.

---

## Directory Structure

```
agent-kg/
├── src/agent_kg/
│   ├── __init__.py
│   ├── models.py      # Python dataclasses — what a Node and Observation look like
│   ├── db.py          # SQLite layer — all reads and writes happen here
│   └── server.py      # MCP surface — tools Claude can call
├── data/              # Created at runtime, gitignored
│   └── agent_kg.db
└── .venv/
```

Three source files. `models.py` has no dependencies. `db.py` imports `models.py`. `server.py` imports `db.py`. Dependency arrow goes one direction only.

---

## Schema

Three tables. No edges yet.

```sql
CREATE TABLE nodes (
    id         TEXT PRIMARY KEY,
    type       TEXT NOT NULL,   -- 'project' | 'session'
    label      TEXT NOT NULL,
    body       TEXT,            -- free-form content; session summary goes here
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE observations (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES nodes(id),
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE obs_fts USING fts5(content, content=observations, content_rowid=rowid);
```

**Why FTS5:** SQLite's built-in full-text search. No extra dependency, fast enough for thousands of records, gives us ranked search immediately. Can swap to vector search later if needed.

**Why no edges yet:** We don't have real traversal queries. Adding edges before we know what relationships we'll actually query is design fiction.

---

## The 6 MCP Tools

| Tool | Signature | Returns |
|------|-----------|---------|
| ping | `ping()` | `"pong"` |
| start_session | `start_session(project, objective)` | `session_id` |
| record_observation | `record_observation(session_id, content)` | `observation_id` |
| end_session | `end_session(session_id, summary)` | `void` |
| search | `search(query, limit=10)` | list of matching records |
| get_context | `get_context(project)` | last N sessions + recent observations |

### What each tool does

**`start_session`** — Creates a `session` node linked to a `project` node (creating the project if it doesn't exist). Returns a `session_id` the agent uses for everything in this session. The objective is stored as the node label (e.g. "Refactor auth middleware"). The session itself is a retrievable entity — this is the novel part.

**`record_observation`** — Appends a raw text record to the observations table, tied to the session. No processing. Think of it as `console.log` for agent context. The agent calls this when it discovers something worth remembering: a decision made, a file's purpose, a constraint found.

**`end_session`** — Writes a summary into the session node's `body` field and marks it complete. The summary is what future sessions retrieve quickly; the raw observations are the full log for when you need detail.

**`search`** — Full-text search over both node labels/bodies and observation content. Returns mixed results ranked by relevance. This is the flat retrieval path.

**`get_context`** — The main tool. Called at the start of a new session for a given project. Returns: the last 3–5 session summaries + the project's most recent observations. This is what solves cross-session amnesia.

---

## Implementation Order

### Step A — `models.py`

Two dataclasses: `Node` and `Observation`. Pure Python, no SQLite imports. Forces us to think about the data shape before touching the database.

### Step B — `db.py`

Five functions:

- `init_db(path)` — creates the file and tables if they don't exist
- `upsert_node(node)` — insert or update a node
- `create_observation(obs)` — insert an observation and update the FTS index
- `search(query, limit)` — FTS query, returns mixed results
- `get_context(project_label)` — returns recent sessions + observations for a project

### Step C — `server.py`

Wire the 6 MCP tools to `db.py`. Each tool is ~5–10 lines. The only logic here is ID generation (`uuid4`) and timestamps (`datetime.utcnow().isoformat()`).

### Step D — Manual test

Run the server. Call each tool in sequence via a Claude Code session. Verify the data appears in the SQLite file:

```bash
sqlite3 data/agent_kg.db "SELECT * FROM nodes;"
sqlite3 data/agent_kg.db "SELECT * FROM observations;"
```

---

## What v2 Adds (when we're ready)

Using v1 for 1–2 weeks of real work will tell us:

- Which observations are actually worth recording vs. noise
- What `get_context` output is genuinely useful vs. overwhelming
- When we first feel the need for "how does X relate to Y across sessions" — that's the moment edges become worth adding
- Whether FTS is good enough or we need semantic similarity

The schema is designed so v2 additions (edge table, multi-key index, promotion rules) are additive — no migration needed on existing data.

### Likely v2 additions

- `edges` table with dual timestamps (ATOM pattern: `observation_at` vs `valid_from`/`valid_until`)
- `keys` table for multi-key indexing per node (+9.4% recall from LongMemEval findings)
- Observation promotion rules (recurrence threshold or explicit user confirmation)
- Hybrid routing: graph traversal for relational queries, flat FTS for content queries

---

## Research Grounding

These design choices are backed by adversarially verified findings (114 agents, 25 verified, 5 confirmed):

| Decision | Finding |
|----------|---------|
| Graph structure planned from day 1 | Flat/vector retrieval provably fails on multi-hop relational queries |
| Multi-key index planned for v2 | +9.4% recall, +5.4% QA accuracy over raw-text-only keys (LongMemEval, ICLR 2025) |
| Human-confirmed graph construction | Automated pipelines are fundamentally unreliable (Microsoft GraphRAG, LightRAG) |
| Hybrid retrieval planned for v2 | Graph fails on full-source/exhaustive queries; flat wins there |
| Session objectives as first-class nodes | No existing system does this — confirmed gap in the literature |
