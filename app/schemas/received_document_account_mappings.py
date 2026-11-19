from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ReceivedDocumentAccountMappingsBase(BaseModel):
    identification_number: str = Field(min_length=1, max_length=80)
    company_name: str = ""
    accounting_account_id: int
    notes: str | None = None
    is_active: bool = True


class ReceivedDocumentAccountMappingsCreate(ReceivedDocumentAccountMappingsBase):
    pass


class ReceivedDocumentAccountMappingsUpdate(BaseModel):
    identification_number: str | None = Field(default=None, min_length=1, max_length=80)
    company_name: str | None = None
    accounting_account_id: int | None = None
    notes: str | None = None
    is_active: bool | None = None


class ReceivedDocumentAccountMappingsOut(ReceivedDocumentAccountMappingsBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_code: str = ""
    account_name: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
