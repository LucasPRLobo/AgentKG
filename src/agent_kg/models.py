from datetime import datetime
from typing import Literal
import pydantic

class Node(pydantic.BaseModel):
    id: str
    type: Literal['project', 'session']
    label: str
    body:str | None =None
    project_id: str | None = None
    status: str | None = None
    created_at: datetime
    updated_at: datetime

class Observation(pydantic.BaseModel):
    id: str
    session_id: str
    content: str
    created_at: datetime
    scope: str
    agent_id: str | None = None
    promoted_to: str | None = None

class Fact(pydantic.BaseModel):
    id: str
    scope: str 
    statement: str 
    subject: str | None = None
    predicate: str | None = None
    object: str | None = None
    t_created : datetime
    t_invalid: datetime | None = None
    valid_from:  datetime | None = None
    valid_until: datetime | None = None
    confidence: float = 1.0
    recurrence_count: int = 1
    last_confirmed_at: datetime
    supersedes: str | None = None
    created_at: datetime
    updated_at: datetime
