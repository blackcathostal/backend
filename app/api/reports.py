"""Accounting reports: EERR (income statement) and consolidated by month."""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import extract, func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.accounting_accounts import AccountingAccounts
from app.models.tax_documents import TaxDocuments
from app.models.users import Users
from app.schemas.reports import (
    EerrAccountRow,
    EerrConsolidatedOut,
    EerrConsolidatedRow,
    EerrReportOut,
)
from app.services import dte_catalog

router = APIRouter(prefix="/reports", tags=["reports"])

_MONTH_LABELS_ES = (
    "",
    "Ene",
    "Feb",
    "Mar",
    "Abr",
    "May",
    "Jun",
    "Jul",
    "Ago",
    "Sep",
    "Oct",
    "Nov",
    "Dic",
)


def _parse_period(value: str | None, *, fallback: date | None = None) -> date:
    """Parse YYYY-MM or MM-YYYY into the first day of that month."""
    text = str(value or "").strip()
    if not text and fallback is not None:
        return date(fallback.year, fallback.month, 1)
    if not text:
        today = date.today()
        return date(today.year, today.month, 1)
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})", text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return date(year, month, 1)
    match = re.fullmatch(r"(\d{1,2})[-/](\d{4})", text)
    if match:
        month, year = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return date(year, month, 1)
    try:
        parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        return date(parsed.year, parsed.month, 1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Periodo inválido (usa YYYY-MM)") from exc


def _period_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _period_label(day: date) -> str:
    return f"{_MONTH_LABELS_ES[day.month]} {day.year}"


def _month_end(day: date) -> date:
    return date(day.year, day.month, monthrange(day.year, day.month)[1])


def _iter_months(start: date, end: date) -> list[date]:
    if end < start:
        start, end = end, start
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    months: list[date] = []
    while cursor <= last:
        months.append(cursor)
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return months


def _income_accounts(db: Session) -> list[AccountingAccounts]:
    rows = (
        db.query(AccountingAccounts)
        .filter(
            AccountingAccounts.is_active.is_(True),
            AccountingAccounts.account_type == "ingreso",
        )
        .order_by(
            AccountingAccounts.sort_order.asc(),
            AccountingAccounts.code.asc(),
        )
        .all()
    )
    # Prefer category Ingresos when present; keep other ingreso accounts after.
    ingresos = [row for row in rows if (row.category or "").strip().lower() == "ingresos"]
    others = [row for row in rows if (row.category or "").strip().lower() != "ingresos"]
    return ingresos + others


def _issued_version_id(db: Session) -> int:
    version_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_ISSUED)
    if version_id is None:
        raise HTTPException(status_code=500, detail="Catálogo dte_versions incompleto")
    return int(version_id)


@router.get("/eerr", response_model=EerrReportOut)
def get_eerr_report(
    period: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> EerrReportOut:
    """Income statement for one month: Ingresos accounts + total per account."""
    month_start = _parse_period(period)
    month_finish = _month_end(month_start)
    issued_id = _issued_version_id(db)
    accounts = _income_accounts(db)

    sums = (
        db.query(
            TaxDocuments.accounting_account_id,
            func.coalesce(func.sum(TaxDocuments.total_amount), 0.0),
        )
        .filter(
            TaxDocuments.dte_version_id == issued_id,
            TaxDocuments.accounting_account_id.isnot(None),
            TaxDocuments.issued_at.isnot(None),
            TaxDocuments.issued_at >= month_start,
            TaxDocuments.issued_at <= month_finish,
        )
        .group_by(TaxDocuments.accounting_account_id)
        .all()
    )
    by_account = {int(account_id): float(total or 0) for account_id, total in sums if account_id}

    rows = [
        EerrAccountRow(
            account_id=int(account.id),
            code=account.code or "",
            name=account.name or "",
            total=float(by_account.get(int(account.id), 0.0)),
        )
        for account in accounts
    ]
    grand_total = float(sum(row.total for row in rows))
    return EerrReportOut(
        period=_period_key(month_start),
        section_title="Ingresos",
        rows=rows,
        grand_total=grand_total,
    )


@router.get("/eerr/consolidated", response_model=EerrConsolidatedOut)
def get_eerr_consolidated(
    from_period: str | None = None,
    to_period: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> EerrConsolidatedOut:
    """Income statement by account with one total column per month."""
    today = date.today()
    default_to = date(today.year, today.month, 1)
    if today.month == 1:
        default_from = date(today.year - 1, 1, 1)
    else:
        default_from = date(today.year, 1, 1)

    start = _parse_period(from_period, fallback=default_from)
    end = _parse_period(to_period, fallback=default_to)
    if end < start:
        start, end = end, start

    months = _iter_months(start, end)
    period_keys = [_period_key(month) for month in months]
    period_labels = [_period_label(month) for month in months]
    issued_id = _issued_version_id(db)
    accounts = _income_accounts(db)

    range_start = months[0]
    range_end = _month_end(months[-1])
    year_col = extract("year", TaxDocuments.issued_at)
    month_col = extract("month", TaxDocuments.issued_at)
    sums = (
        db.query(
            TaxDocuments.accounting_account_id,
            year_col,
            month_col,
            func.coalesce(func.sum(TaxDocuments.total_amount), 0.0),
        )
        .filter(
            TaxDocuments.dte_version_id == issued_id,
            TaxDocuments.accounting_account_id.isnot(None),
            TaxDocuments.issued_at.isnot(None),
            TaxDocuments.issued_at >= range_start,
            TaxDocuments.issued_at <= range_end,
        )
        .group_by(
            TaxDocuments.accounting_account_id,
            year_col,
            month_col,
        )
        .all()
    )

    totals: dict[tuple[int, str], float] = {}
    for account_id, year, month, total in sums:
        if account_id is None or year is None or month is None:
            continue
        key = (int(account_id), f"{int(year):04d}-{int(month):02d}")
        totals[key] = float(total or 0)

    rows: list[EerrConsolidatedRow] = []
    column_totals = {key: 0.0 for key in period_keys}
    for account in accounts:
        amounts = {
            key: float(totals.get((int(account.id), key), 0.0)) for key in period_keys
        }
        row_total = float(sum(amounts.values()))
        for key, value in amounts.items():
            column_totals[key] = float(column_totals.get(key, 0.0) + value)
        rows.append(
            EerrConsolidatedRow(
                account_id=int(account.id),
                code=account.code or "",
                name=account.name or "",
                amounts=amounts,
                total=row_total,
            )
        )

    return EerrConsolidatedOut(
        from_period=_period_key(start),
        to_period=_period_key(end),
        periods=period_keys,
        period_labels=period_labels,
        section_title="Ingresos",
        rows=rows,
        column_totals=column_totals,
        grand_total=float(sum(column_totals.values())),
    )
