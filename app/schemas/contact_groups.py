from datetime import datetime

from pydantic import BaseModel, Field


class ContactGroupsCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    is_active: bool = True


class ContactGroupsUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    is_active: bool | None = None


class ContactGroupsOut(BaseModel):
    id: int
    name: str
    description: str | None = ""
    is_active: bool
    created_at: datetime | None = None

    model_config = {"from_attributes": True}
