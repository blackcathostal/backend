from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

AccountType = Literal["activo", "pasivo", "patrimonio", "ingreso", "gasto"]
AccountNature = Literal["deudora", "acreedora"]


class AccountingAccountsCreate(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=180)
    account_type: AccountType
    nature: AccountNature = "deudora"
    description: str = ""
    is_active: bool = True


class AccountingAccountsUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=40)
    name: str | None = Field(default=None, min_length=1, max_length=180)
    account_type: AccountType | None = None
    nature: AccountNature | None = None
    description: str | None = None
    is_active: bool | None = None


class AccountingAccountsOut(BaseModel):
    id: int
    code: str
    name: str
    account_type: str
    nature: str
    description: str | None = ""
    is_active: bool
    created_at: datetime | None = None

    model_config = {"from_attributes": True}
