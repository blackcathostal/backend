from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class RoomsCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    type: str = Field(min_length=1, max_length=80)
    capacity: int = Field(ge=1)
    price: int = Field(ge=0)
    features: str = Field(default="", max_length=4000)


class RoomsUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    type: str | None = Field(default=None, min_length=1, max_length=80)
    capacity: int | None = Field(default=None, ge=1)
    price: int | None = Field(default=None, ge=0)
    features: str | None = Field(default=None, max_length=4000)


class RoomsOut(BaseModel):
    id: int
    name: str
    type: str
    capacity: int
    price: int
    features: str = ""
    photos: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}

    @field_validator("photos", mode="before")
    @classmethod
    def _parse_photos(cls, value: Any) -> list[str]:
        if value is None or value == "":
            return [""] * 6
        if isinstance(value, list):
            items = [str(item or "").strip() for item in value][:6]
            while len(items) < 6:
                items.append("")
            return items
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return [""] * 6
            try:
                import json

                loaded = json.loads(text)
            except json.JSONDecodeError:
                return ([text] + [""] * 5)[:6] if text.startswith("/") else [""] * 6
            if isinstance(loaded, list):
                items = [str(item or "").strip() for item in loaded][:6]
                while len(items) < 6:
                    items.append("")
                return items
        return [""] * 6
