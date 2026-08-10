from datetime import datetime

from pydantic import BaseModel


class MediasOut(BaseModel):
    id: int
    filename: str
    url: str
    category: str
    alt_text: str
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class MediasUpdate(BaseModel):
    alt_text: str | None = None
    category: str | None = None
