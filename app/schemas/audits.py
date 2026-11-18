from datetime import datetime

from pydantic import BaseModel, Field


class AuditCreate(BaseModel):
    event_type: str = Field(default="navigation", max_length=40)
    action: str = Field(default="", max_length=120)
    path: str = Field(default="", max_length=500)
    screen: str = Field(default="", max_length=200)
    detail: str = ""
    duration_ms: int = 0


class AuditOut(BaseModel):
    id: int
    user_id: int | None = None
    user_email: str = ""
    event_type: str
    action: str
    path: str
    screen: str
    detail: str = ""
    duration_ms: int = 0
    created_at: datetime | None = None

    model_config = {"from_attributes": True}
