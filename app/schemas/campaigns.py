from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field


class CampaignAttachment(BaseModel):
    name: str
    size: int = 0
    path: str | None = None
    url: str | None = None
    content_type: str | None = None


class CampaignRecipient(BaseModel):
    id: int | None = None
    full_name: str = ""
    email: EmailStr | str


class CampaignsCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    from_email: EmailStr
    subject: str = Field(min_length=1, max_length=255)
    html_body: str = Field(min_length=1)
    status: str = "Borrador"
    sent: int = 0
    recipients: list[CampaignRecipient] = Field(default_factory=list)
    attachments: list[CampaignAttachment] = Field(default_factory=list)


class CampaignsUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    from_email: EmailStr | None = None
    subject: str | None = Field(default=None, min_length=1, max_length=255)
    html_body: str | None = Field(default=None, min_length=1)
    status: str | None = None
    sent: int | None = None
    recipients: list[CampaignRecipient] | None = None
    attachments: list[CampaignAttachment] | None = None
    sent_at: datetime | None = None


class CampaignsOut(BaseModel):
    id: int
    name: str
    from_email: EmailStr | str
    subject: str
    html_body: str
    status: str
    sent: int
    recipients: list[Any] = Field(default_factory=list)
    attachments: list[Any] = Field(default_factory=list)
    sent_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
