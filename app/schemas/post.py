from datetime import datetime

from pydantic import BaseModel, Field


class PostsBase(BaseModel):
    slug: str = Field(min_length=1, max_length=220)
    title: str = Field(min_length=1, max_length=220)
    keywords: str = Field(default="", max_length=500)
    excerpt: str = ""
    body: str = ""
    category: str = "Blog"
    image_url: str = ""
    author: str = "Black Cat Hostal"
    sort_order: int = 0
    is_active: bool = True
    published_at: datetime | None = None


class PostsCreate(PostsBase):
    pass


class PostsUpdate(BaseModel):
    slug: str | None = Field(default=None, min_length=1, max_length=220)
    title: str | None = Field(default=None, min_length=1, max_length=220)
    keywords: str | None = Field(default=None, max_length=500)
    excerpt: str | None = None
    body: str | None = None
    category: str | None = None
    image_url: str | None = None
    author: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    published_at: datetime | None = None


class PostsOut(PostsBase):
    id: int
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
