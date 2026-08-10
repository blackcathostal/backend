from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class ContactsCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=160)
    email: EmailStr
    is_active: bool = True
    group_id: int | None = None


class ContactsUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=160)
    email: EmailStr | None = None
    is_active: bool | None = None
    group_id: int | None = None


class ContactsOut(BaseModel):
    id: int
    full_name: str
    email: EmailStr
    is_active: bool
    group_id: int | None = None
    group_name: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}
