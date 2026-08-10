from datetime import datetime

from pydantic import BaseModel, Field


class SlidersBase(BaseModel):
    eyebrow: str = ""
    title: str
    image_url: str
    overlay: int = Field(default=3, ge=0, le=9)
    sort_order: int = 0
    is_active: bool = True


class SlidersCreate(SlidersBase):
    pass


class SlidersUpdate(BaseModel):
    eyebrow: str | None = None
    title: str | None = None
    image_url: str | None = None
    overlay: int | None = Field(default=None, ge=0, le=9)
    sort_order: int | None = None
    is_active: bool | None = None


class SlidersOut(SlidersBase):
    id: int
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class SlidersReorderItem(BaseModel):
    id: int
    sort_order: int
