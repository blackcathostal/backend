from datetime import datetime

from pydantic import BaseModel, Field


class RoomsCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    type: str = Field(min_length=1, max_length=80)
    capacity: int = Field(ge=1)
    price: int = Field(ge=0)
    status: str = Field(default="Disponible", max_length=40)


class RoomsUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    type: str | None = Field(default=None, min_length=1, max_length=80)
    capacity: int | None = Field(default=None, ge=1)
    price: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, max_length=40)


class RoomsOut(BaseModel):
    id: int
    name: str
    type: str
    capacity: int
    price: int
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
