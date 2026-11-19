from __future__ import annotations

from pydantic import BaseModel, Field


class EerrAccountRow(BaseModel):
    account_id: int | None = None
    code: str = ""
    name: str = ""
    total: float = 0
    # account = chart row; pending / unclassified = remanente detail (not a CoA dump)
    row_kind: str = "account"


class EerrReportOut(BaseModel):
    period: str
    section_title: str = "Ingresos"
    rows: list[EerrAccountRow] = Field(default_factory=list)
    grand_total: float = 0
    # Chilean VAT breakdown of grand_total (bruto): neto + IVA ≈ bruto.
    grand_net: float = 0
    grand_tax: float = 0
    vendido: float = 0
    cobrado: float = 0
    pendiente: float = 0
    facturado: float = 0
    concepts_total: float = 0
    pending_sales: float = 0
    unclassified_collections: float = 0
    source: str = "extranet_vendido"


class EerrConsolidatedRow(BaseModel):
    account_id: int | None = None
    code: str = ""
    name: str = ""
    amounts: dict[str, float] = Field(default_factory=dict)
    total: float = 0
    row_kind: str = "account"


class EerrConsolidatedOut(BaseModel):
    from_period: str
    to_period: str
    periods: list[str] = Field(default_factory=list)
    period_labels: list[str] = Field(default_factory=list)
    section_title: str = "Ingresos"
    rows: list[EerrConsolidatedRow] = Field(default_factory=list)
    column_totals: dict[str, float] = Field(default_factory=dict)
    grand_total: float = 0
    source: str = "extranet_vendido"


class EerrDetailLine(BaseModel):
    issued_at: str | None = None
    folio: str = ""
    doc_type: str = ""
    company_name: str = ""
    locator: str = ""
    concept: str = ""
    amount: float = 0


class EerrDetailOut(BaseModel):
    period: str
    row_kind: str = "account"
    account_id: int | None = None
    account_code: str = ""
    account_name: str = ""
    title: str = ""
    total: float = 0
    lines: list[EerrDetailLine] = Field(default_factory=list)
