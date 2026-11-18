from datetime import datetime

from pydantic import BaseModel, Field


class PermissionOut(BaseModel):
    id: int
    code: str
    label: str
    section: str = ""
    description: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class PermissionCreate(BaseModel):
    code: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    section: str = Field(default="", max_length=80)
    description: str = ""


class PermissionUpdate(BaseModel):
    code: str | None = Field(default=None, max_length=80)
    label: str | None = Field(default=None, max_length=120)
    section: str | None = Field(default=None, max_length=80)
    description: str | None = None


class RoleOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    permission_ids: list[int] = []
    permissions: list[PermissionOut] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = None
    permission_ids: list[int] = []


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    description: str | None = None
    permission_ids: list[int] | None = None
