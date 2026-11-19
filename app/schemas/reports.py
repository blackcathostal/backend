from __future__ import annotations

from pydantic import BaseModel, Field


class EerrAccountRow(BaseModel):
    account_id: int
    code: str = ""
    name: str = ""
    total: float = 0


class EerrReportOut(BaseModel):
    period: str
    section_title: str = "Ingresos"
    rows: list[EerrAccountRow] = Field(default_factory=list)
    grand_total: float = 0


class EerrConsolidatedRow(BaseModel):
    account_id: int
    code: str = ""
    name: str = ""
    amounts: dict[str, float] = Field(default_factory=dict)
    total: float = 0


class EerrConsolidatedOut(BaseModel):
    from_period: str
    to_period: str
    periods: list[str] = Field(default_factory=list)
    period_labels: list[str] = Field(default_factory=list)
    section_title: str = "Ingresos"
    rows: list[EerrConsolidatedRow] = Field(default_factory=list)
    column_totals: dict[str, float] = Field(default_factory=dict)
    grand_total: float = 0
