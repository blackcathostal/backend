from datetime import datetime

from pydantic import BaseModel, Field


class ServicesCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=120)
    price: str = Field(min_length=1, max_length=80)
    status: str = Field(default="Activo", max_length=40)
    description: str | None = None


class ServicesUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    category: str | None = Field(default=None, min_length=1, max_length=120)
    price: str | None = Field(default=None, min_length=1, max_length=80)
    status: str | None = Field(default=None, max_length=40)
    description: str | None = None


class ServicesOut(BaseModel):
    id: int
    name: str
    category: str
    price: str
    status: str
    description: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
