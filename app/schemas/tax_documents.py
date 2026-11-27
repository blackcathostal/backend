from datetime import date, datetime

from pydantic import BaseModel, Field


class DteStatusOut(BaseModel):
    id: int
    code: str
    name: str
    tip_color: str = "red"

    model_config = {"from_attributes": True}


class DteTypeOut(BaseModel):
    id: int
    code: str
    name: str

    model_config = {"from_attributes": True}


class DteVersionOut(BaseModel):
    id: int
    code: str
    name: str

    model_config = {"from_attributes": True}


class TaxDocumentOut(BaseModel):
    id: int
    dte_type_id: int | None = None
    dte_type_code: str = ""
    dte_type_name: str = ""
    dte_version_id: int
    dte_version_code: str = ""
    dte_version_name: str = ""
    status_id: int | None = None
    status_name: str = ""
    status_code: str = ""
    status_tip_color: str = "red"
    accounting_account_id: int | None = None
    accounting_account_code: str = ""
    accounting_account_name: str = ""
    period: str = ""
    folio: str
    identification_number: str = ""
    company_name: str = ""
    issued_at: date | None = None
    net_amount: float = 0
    exempt_amount: float = 0
    tax_amount: float = 0
    total_amount: float = 0
    branch: str = ""
    pms_class: str = "Sin match"
    pms_ref: str = ""
    source: str = "api"
    has_pdf: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class TaxDocumentPatch(BaseModel):
    status_id: int | None = None
    period: str | None = Field(default=None, max_length=40)
    accounting_account_id: int | None = None


class TaxDocumentCreate(BaseModel):
    """Manual Track entry (issued or received)."""

    direction: str = Field(description="issued or received")
    dte_type_code: str = Field(description="Catalog code: 33, 39, voucher, …")
    total_amount: float = Field(ge=0)
    identification_number: str = Field(default="", max_length=80)
    company_name: str = Field(default="", max_length=200)
    issued_at: date | None = None
    folio: str = Field(default="", max_length=40)
    period: str | None = Field(default=None, max_length=40)
    accounting_account_id: int | None = None
    status_id: int | None = None


class TaxDocumentRefreshResult(BaseModel):
    dte_version_id: int
    fetched: int
    upserted: int
    classified: int
    suppliers_upserted: int = 0
    customers_upserted: int = 0
    accounts_assigned: int = 0
    warning: str = ""


class TaxDocumentClearResult(BaseModel):
    dte_version_id: int
    deleted: int


class TaxDocumentListResult(BaseModel):
    items: list[TaxDocumentOut]
    total: int
    from_date: date | None = None
    to_date: date | None = None
    statuses: list[DteStatusOut] = []
