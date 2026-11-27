from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.services.accounting_concept_map import parse_concept_labels

AccountType = Literal["activo", "pasivo", "patrimonio", "ingreso", "gasto"]
AccountNature = Literal["deudora", "acreedora"]
AccountCategory = Literal[
    "Ingresos",
    "Mantencion",
    "Materiales",
    "Remuneraciones",
    "Servicios",
    "Comisiones",
    "Activo Fijo",
    "Balance",
]


class AccountingAccountsCreate(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=180)
    category: AccountCategory | str = "Balance"
    account_type: AccountType
    nature: AccountNature = "deudora"
    sort_order: int = 0
    description: str = ""
    concept_labels: list[str] | str = Field(default_factory=list)
    is_active: bool = True


class AccountingAccountsUpdate(BaseModel):
    code: str | None = Field(default=None, min_length=1, max_length=40)
    name: str | None = Field(default=None, min_length=1, max_length=180)
    category: AccountCategory | str | None = None
    account_type: AccountType | None = None
    nature: AccountNature | None = None
    sort_order: int | None = None
    description: str | None = None
    concept_labels: list[str] | str | None = None
    is_active: bool | None = None


class AccountingAccountsOut(BaseModel):
    id: int
    code: str
    name: str
    category: str = ""
    account_type: str
    nature: str
    sort_order: int = 0
    description: str | None = ""
    concept_labels: list[str] = Field(default_factory=list)
    is_active: bool
    created_at: datetime | None = None

    model_config = {"from_attributes": True}

    @field_validator("concept_labels", mode="before")
    @classmethod
    def _parse_labels(cls, value: object) -> list[str]:
        return parse_concept_labels(value)


class AccountingAccountsImportResult(BaseModel):
    created: int
    updated: int
    total: int
