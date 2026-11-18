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
    dte_version_id: int
    dte_version_code: str = ""
    dte_version_name: str = ""
    dte_type_id: int | None = None
    dte_type_code: str = ""
    dte_type_name: str = ""
    status_id: int | None = None
    status_name: str = ""
    status_code: str = ""
    status_tip_color: str = "red"
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


class TaxDocumentRefreshResult(BaseModel):
    dte_version_id: int
    fetched: int
    upserted: int
    classified: int
    suppliers_upserted: int = 0
    warning: str = ""


class TaxDocumentListResult(BaseModel):
    items: list[TaxDocumentOut]
    total: int
    from_date: date | None = None
    to_date: date | None = None
    statuses: list[DteStatusOut] = []
