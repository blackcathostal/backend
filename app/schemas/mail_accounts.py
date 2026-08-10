from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class SignatureImageOut(BaseModel):
    name: str
    size: int = 0
    path: str
    url: str
    content_type: str = "application/octet-stream"


class MailAccountsCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=1, max_length=255)
    smtp_host: str = Field(min_length=1, max_length=255)
    smtp_port: str = Field(default="587", max_length=10)
    imap_host: str = Field(min_length=1, max_length=255)
    imap_port: str = Field(default="993", max_length=10)
    use_ssl: bool = False
    is_active: bool = True
    is_default: bool = False
    signature: str = ""


class MailAccountsUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=1, max_length=255)
    smtp_host: str | None = Field(default=None, min_length=1, max_length=255)
    smtp_port: str | None = Field(default=None, max_length=10)
    imap_host: str | None = Field(default=None, min_length=1, max_length=255)
    imap_port: str | None = Field(default=None, max_length=10)
    use_ssl: bool | None = None
    is_active: bool | None = None
    is_default: bool | None = None
    signature: str | None = None


class MailAccountsOut(BaseModel):
    id: int
    name: str
    email: EmailStr
    password: str
    smtp_host: str
    smtp_port: str
    imap_host: str
    imap_port: str
    use_ssl: bool
    is_active: bool
    is_default: bool
    signature: str | None = ""
    created_at: datetime | None = None

    model_config = {"from_attributes": True}
