from datetime import date, datetime

from pydantic import BaseModel, Field


class AccountingTrackDocumentOut(BaseModel):
    id: int
    direction: str
    doc_type: str
    folio: str
    counterpart_rut: str
    counterpart_name: str
    issued_at: date | None = None
    net_amount: float = 0
    exempt_amount: float = 0
    tax_amount: float = 0
    total_amount: float = 0
    branch: str = ""
    pms_class: str = "Sin match"
    pms_ref: str = ""
    source: str = "api"
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class AccountingTrackRefreshResult(BaseModel):
    direction: str
    fetched: int
    upserted: int
    classified: int
    warning: str = ""


class AccountingTrackListResult(BaseModel):
    items: list[AccountingTrackDocumentOut]
    total: int
    from_date: date | None = None
    to_date: date | None = None
