from datetime import datetime

from pydantic import BaseModel, Field


class TermsOut(BaseModel):
    id: int
    locale: str
    title: str
    intro: str
    body_html: str
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class TermsUpdate(BaseModel):
    title: str = Field(min_length=2, max_length=220)
    intro: str = Field(default="", max_length=4000)
    body_html: str = Field(min_length=1, max_length=200_000)
