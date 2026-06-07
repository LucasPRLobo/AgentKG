from datetime import datetime
from typing import Literal
import pydantic

class Node(pydantic.BaseModel):
    id: str
    type: Literal['project', 'session']
    label: str
    body:str | None =None
    project_id: str | None = None
    created_at: datetime
    updated_at: datetime

class Observation(pydantic.BaseModel):
    id: str
    session_id: str
    content: str
    created_at: datetime

