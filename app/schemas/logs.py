from datetime import datetime

from pydantic import BaseModel, Field


class LogCreate(BaseModel):
    source: str = Field(default="frontend", max_length=40)
    level: str = Field(default="error", max_length=20)
    message: str = ""
    stack: str = ""
    path: str = Field(default="", max_length=500)
    meta_json: str = ""


class LogOut(BaseModel):
    id: int
    source: str
    level: str
    message: str
    stack: str = ""
    path: str = ""
    user_id: int | None = None
    user_email: str = ""
    meta_json: str = ""
    created_at: datetime | None = None

    model_config = {"from_attributes": True}
