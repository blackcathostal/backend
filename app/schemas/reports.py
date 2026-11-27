from __future__ import annotations

from pydantic import BaseModel, Field


class EerrAccountRow(BaseModel):
    account_id: int | None = None
    code: str = ""
    name: str = ""
    total: float = 0
    # account = chart row; pending / unclassified = remainder detail (not a CoA dump)
    row_kind: str = "account"
    # Expense grouping label from accounting_accounts.category (UI may be Spanish).
    category: str = ""


class EerrReportOut(BaseModel):
    period: str
    section_title: str = "Ingresos"
    rows: list[EerrAccountRow] = Field(default_factory=list)
    grand_total: float = 0
    # Chilean VAT breakdown of grand_total (gross): net + tax ≈ gross.
    grand_net: float = 0
    grand_tax: float = 0
    expense_section_title: str = "Egresos"
    expense_rows: list[EerrAccountRow] = Field(default_factory=list)
    expenses_total: float = 0
    result_total: float = 0
    sold: float = 0
    collected: float = 0
    pending: float = 0
    invoiced: float = 0
    concepts_total: float = 0
    pending_sales: float = 0
    unclassified_collections: float = 0
    source: str = "tax_documents_issued"


class EerrConsolidatedRow(BaseModel):
    account_id: int | None = None
    code: str = ""
    name: str = ""
    amounts: dict[str, float] = Field(default_factory=dict)
    total: float = 0
    row_kind: str = "account"
    category: str = ""


class EerrConsolidatedOut(BaseModel):
    from_period: str
    to_period: str
    periods: list[str] = Field(default_factory=list)
    period_labels: list[str] = Field(default_factory=list)
    section_title: str = "Ingresos"
    rows: list[EerrConsolidatedRow] = Field(default_factory=list)
    # Income bruto per month (= monthly EERR grand_total / Emitido).
    column_totals: dict[str, float] = Field(default_factory=dict)
    column_net_totals: dict[str, float] = Field(default_factory=dict)
    column_tax_totals: dict[str, float] = Field(default_factory=dict)
    grand_total: float = 0
    grand_net: float = 0
    grand_tax: float = 0
    expense_section_title: str = "Egresos"
    expense_rows: list[EerrConsolidatedRow] = Field(default_factory=list)
    column_expense_totals: dict[str, float] = Field(default_factory=dict)
    expenses_total: float = 0
    # Resultado = neto − egresos (same as monthly EERR).
    column_result_totals: dict[str, float] = Field(default_factory=dict)
    result_total: float = 0
    # Periods present in eerr_month_snapshots (opened monthly EERR at least once).
    cached_periods: list[str] = Field(default_factory=list)
    missing_periods: list[str] = Field(default_factory=list)
    source: str = "eerr_month_snapshots"


class EerrDetailLine(BaseModel):
    issued_at: str | None = None
    folio: str = ""
    doc_type: str = ""
    company_name: str = ""
    locator: str = ""
    concept: str = ""
    amount: float = 0
    tax_document_id: int | None = None
    has_pdf: bool = False


class EerrDetailOut(BaseModel):
    period: str
    row_kind: str = "account"
    account_id: int | None = None
    account_code: str = ""
    account_name: str = ""
    title: str = ""
    total: float = 0
    lines: list[EerrDetailLine] = Field(default_factory=list)


class OccupancyDayRow(BaseModel):
    date: str
    weekday: str = ""
    rooms_occupied: int = 0
    guests: int = 0
    hosts: int = 0
    check_in: int = 0
    check_out: int = 0
    occupancy_pct: float | None = None


class OccupancyRoomTypeRow(BaseModel):
    room_type: str
    capacity: int | None = None
    daily_pct: dict[str, float] = Field(default_factory=dict)
    average_pct: float | None = None
    is_total: bool = False


class OccupancyReportOut(BaseModel):
    period: str
    day: str | None = None
    room_type: str | None = None
    available_room_types: list[str] = Field(default_factory=list)
    total_rooms: int | None = None
    month_avg_occupancy_pct: float | None = None
    days: list[OccupancyDayRow] = Field(default_factory=list)
    room_types: list[OccupancyRoomTypeRow] = Field(default_factory=list)
    refreshed_at: str | None = None
    has_data: bool = False
    source: str = "occupancy_stats_db"


class OccupancyRoomTypeMonthRow(BaseModel):
    room_type: str
    capacity: int | None = None
    monthly_pct: dict[str, float | None] = Field(default_factory=dict)
    average_pct: float | None = None


class OccupancyRoomTypesByMonthOut(BaseModel):
    from_period: str
    to_period: str
    periods: list[str] = Field(default_factory=list)
    period_labels: list[str] = Field(default_factory=list)
    rows: list[OccupancyRoomTypeMonthRow] = Field(default_factory=list)
    has_data: bool = False
    source: str = "occupancy_stats_db"
