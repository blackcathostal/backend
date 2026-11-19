"""Accounting reports: EERR (income statement) and consolidated by month."""

from __future__ import annotations

import logging
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
    EerrDetailLine,
    EerrDetailOut,
    EerrReportOut,
)
from app.services import dte_catalog
from app.services.accounting_concept_map import (
    ensure_concept_labels_from_coa,
    load_income_accounts,
)
from app.services.extranet_web_client import ExtranetWebError
from app.services.tax_documents_fetch import TaxDocumentsFetchClient

router = APIRouter(prefix="/reports", tags=["reports"])
logger = logging.getLogger(__name__)

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


def _issued_version_id(db: Session) -> int:
    version_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_ISSUED)
    if version_id is None:
        raise HTTPException(status_code=500, detail="Catálogo dte_versions incompleto")
    return int(version_id)


def _rows_from_code_totals(
    accounts: list[AccountingAccounts],
    by_code: dict[str, float],
) -> list[EerrAccountRow]:
    return [
        EerrAccountRow(
            account_id=int(account.id),
            code=account.code or "",
            name=account.name or "",
            total=float(by_code.get(str(account.code or ""), 0.0)),
            row_kind="account",
        )
        for account in accounts
    ]


def _remanente_rows(
    *,
    pending_sales: float,
    unclassified_collections: float,
) -> list[EerrAccountRow]:
    """Detail lines for vendido not mapped to Cobros concept columns."""
    rows: list[EerrAccountRow] = []
    if abs(pending_sales) > 0.5:
        rows.append(
            EerrAccountRow(
                account_id=None,
                code="",
                name="Pendiente por cobrar",
                total=float(pending_sales),
                row_kind="pending",
            )
        )
    if abs(unclassified_collections) > 0.5:
        rows.append(
            EerrAccountRow(
                account_id=None,
                code="",
                name="Cobros sin clasificar",
                total=float(unclassified_collections),
                row_kind="unclassified",
            )
        )
    return rows


def _tax_document_code_totals(
    db: Session,
    *,
    date_from: date,
    date_to: date,
) -> dict[str, float]:
    """Fallback: sum issued document totals by accounting account code."""
    issued_id = _issued_version_id(db)
    sums = (
        db.query(
            AccountingAccounts.code,
            func.coalesce(func.sum(TaxDocuments.total_amount), 0.0),
        )
        .join(
            AccountingAccounts,
            AccountingAccounts.id == TaxDocuments.accounting_account_id,
        )
        .filter(
            TaxDocuments.dte_version_id == issued_id,
            TaxDocuments.accounting_account_id.isnot(None),
            TaxDocuments.issued_at.isnot(None),
            TaxDocuments.issued_at >= date_from,
            TaxDocuments.issued_at <= date_to,
        )
        .group_by(AccountingAccounts.code)
        .all()
    )
    return {str(code or ""): float(total or 0) for code, total in sums if code}


def _fetch_extranet_month(date_from: date, date_to: date, *, accounts: list) -> dict:
    client = TaxDocumentsFetchClient()
    return client.fetch_income_by_account(date_from, date_to, accounts=accounts)


@router.get("/eerr", response_model=EerrReportOut)
def get_eerr_report(
    period: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> EerrReportOut:
    """Income statement for one month from FNS Vendido (not Cobros/emitidas only)."""
    month_start = _parse_period(period)
    # Cap "this month" to today so mid-month matches FNS open range.
    today = date.today()
    month_finish = min(_month_end(month_start), today) if (
        month_start.year == today.year and month_start.month == today.month
    ) else _month_end(month_start)
    ensure_concept_labels_from_coa(db)
    accounts = load_income_accounts(db)

    source = "extranet_vendido"
    vendido = 0.0
    cobrado = 0.0
    pendiente = 0.0
    concepts_total = 0.0
    pending_sales = 0.0
    unclassified_collections = 0.0
    by_code: dict[str, float] = {}
    try:
        payload = _fetch_extranet_month(month_start, month_finish, accounts=accounts)
        by_code = {
            str(code): float(amount or 0)
            for code, amount in (payload.get("by_account_code") or {}).items()
        }
        vendido = float(payload.get("vendido") or 0)
        cobrado = float(payload.get("cobrado") or 0)
        pendiente = float(payload.get("pendiente") or 0)
        concepts_total = float(payload.get("concepts_total") or 0)
        pending_sales = float(payload.get("pending_sales") or 0)
        unclassified_collections = float(payload.get("unclassified_collections") or 0)
    except ExtranetWebError as exc:
        logger.warning("EERR extranet unavailable, falling back to tax_documents: %s", exc)
        source = "tax_documents"
        by_code = _tax_document_code_totals(
            db, date_from=month_start, date_to=month_finish
        )
        vendido = float(sum(by_code.values()))
        cobrado = vendido
        concepts_total = vendido

    rows = _rows_from_code_totals(accounts, by_code)
    rows.extend(
        _remanente_rows(
            pending_sales=pending_sales,
            unclassified_collections=unclassified_collections,
        )
    )
    rows_total = float(sum(row.total for row in rows))
    if vendido <= 0:
        vendido = rows_total
    grand_total = vendido if vendido > 0 else rows_total
    return EerrReportOut(
        period=_period_key(month_start),
        section_title="Ingresos",
        rows=rows,
        grand_total=grand_total,
        vendido=vendido,
        cobrado=cobrado,
        pendiente=pendiente,
        concepts_total=concepts_total,
        pending_sales=pending_sales,
        unclassified_collections=unclassified_collections,
        source=source,
    )


@router.get("/eerr/detail", response_model=EerrDetailOut)
def get_eerr_detail(
    period: str | None = None,
    row_kind: str = "account",
    account_id: int | None = None,
    account_code: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> EerrDetailOut:
    """Drill-down lines for an EERR account or remanente row."""
    month_start = _parse_period(period)
    today = date.today()
    month_finish = min(_month_end(month_start), today) if (
        month_start.year == today.year and month_start.month == today.month
    ) else _month_end(month_start)

    ensure_concept_labels_from_coa(db)
    accounts = load_income_accounts(db)
    kind = str(row_kind or "account").strip().lower()
    code = str(account_code or "").strip()
    account_name = ""
    resolved_id = account_id

    if kind == "account":
        account = None
        if account_id is not None:
            account = next((row for row in accounts if int(row.id) == int(account_id)), None)
        if account is None and code:
            account = next(
                (row for row in accounts if str(row.code or "") == code),
                None,
            )
        if account is None:
            raise HTTPException(status_code=404, detail="Cuenta contable no encontrada")
        code = str(account.code or "")
        account_name = str(account.name or "")
        resolved_id = int(account.id)
        title = f"{code} · {account_name}".strip(" ·")
    elif kind == "pending":
        title = "Pendiente por cobrar"
    elif kind == "unclassified":
        title = "Cobros sin clasificar"
    else:
        raise HTTPException(status_code=400, detail="row_kind inválido")

    try:
        client = TaxDocumentsFetchClient()
        raw_lines = client.fetch_income_detail_lines(
            month_start,
            month_finish,
            accounts=accounts,
            row_kind=kind,
            account_code=code,
        )
    except ExtranetWebError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    lines = [EerrDetailLine.model_validate(item) for item in raw_lines]
    total = float(sum(float(item.amount or 0) for item in lines))
    return EerrDetailOut(
        period=_period_key(month_start),
        row_kind=kind,
        account_id=resolved_id,
        account_code=code,
        account_name=account_name,
        title=title,
        total=total,
        lines=lines,
    )


@router.get("/eerr/consolidated", response_model=EerrConsolidatedOut)
def get_eerr_consolidated(
    from_period: str | None = None,
    to_period: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> EerrConsolidatedOut:
    """Income statement by account with one total column per month (FNS Vendido)."""
    today = date.today()
    default_to = date(today.year, today.month, 1)
    default_from = date(today.year, 1, 1)

    start = _parse_period(from_period, fallback=default_from)
    end = _parse_period(to_period, fallback=default_to)
    if end < start:
        start, end = end, start

    months = _iter_months(start, end)
    period_keys = [_period_key(month) for month in months]
    period_labels = [_period_label(month) for month in months]
    ensure_concept_labels_from_coa(db)
    accounts = load_income_accounts(db)

    monthly_by_code: dict[str, dict[str, float]] = {key: {} for key in period_keys}
    monthly_pending: dict[str, float] = {key: 0.0 for key in period_keys}
    monthly_unclassified: dict[str, float] = {key: 0.0 for key in period_keys}
    monthly_vendido: dict[str, float] = {key: 0.0 for key in period_keys}
    source = "extranet_vendido"
    try:
        client = TaxDocumentsFetchClient()
        for month in months:
            key = _period_key(month)
            finish = min(_month_end(month), today) if (
                month.year == today.year and month.month == today.month
            ) else _month_end(month)
            payload = client.fetch_income_by_account(
                month, finish, accounts=accounts
            )
            monthly_by_code[key] = {
                str(code): float(amount or 0)
                for code, amount in (payload.get("by_account_code") or {}).items()
            }
            monthly_pending[key] = float(payload.get("pending_sales") or 0)
            monthly_unclassified[key] = float(
                payload.get("unclassified_collections") or 0
            )
            monthly_vendido[key] = float(payload.get("vendido") or 0)
    except ExtranetWebError as exc:
        logger.warning(
            "EERR consolidated extranet unavailable, falling back to tax_documents: %s",
            exc,
        )
        source = "tax_documents"
        issued_id = _issued_version_id(db)
        year_col = extract("year", TaxDocuments.issued_at)
        month_col = extract("month", TaxDocuments.issued_at)
        sums = (
            db.query(
                AccountingAccounts.code,
                year_col,
                month_col,
                func.coalesce(func.sum(TaxDocuments.total_amount), 0.0),
            )
            .join(
                AccountingAccounts,
                AccountingAccounts.id == TaxDocuments.accounting_account_id,
            )
            .filter(
                TaxDocuments.dte_version_id == issued_id,
                TaxDocuments.accounting_account_id.isnot(None),
                TaxDocuments.issued_at.isnot(None),
                TaxDocuments.issued_at >= months[0],
                TaxDocuments.issued_at <= _month_end(months[-1]),
            )
            .group_by(AccountingAccounts.code, year_col, month_col)
            .all()
        )
        monthly_by_code = {key: {} for key in period_keys}
        for code, year, month_num, total in sums:
            if not code or year is None or month_num is None:
                continue
            key = f"{int(year):04d}-{int(month_num):02d}"
            if key not in monthly_by_code:
                continue
            monthly_by_code[key][str(code)] = float(total or 0)
            monthly_vendido[key] = float(
                monthly_vendido.get(key, 0.0) + float(total or 0)
            )

    rows: list[EerrConsolidatedRow] = []
    column_totals = {key: 0.0 for key in period_keys}
    for account in accounts:
        code = str(account.code or "")
        amounts = {
            key: float((monthly_by_code.get(key) or {}).get(code, 0.0))
            for key in period_keys
        }
        row_total = float(sum(amounts.values()))
        for key, value in amounts.items():
            column_totals[key] = float(column_totals.get(key, 0.0) + value)
        rows.append(
            EerrConsolidatedRow(
                account_id=int(account.id),
                code=code,
                name=account.name or "",
                amounts=amounts,
                total=row_total,
                row_kind="account",
            )
        )

    for kind, name, monthly in (
        ("pending", "Pendiente por cobrar", monthly_pending),
        ("unclassified", "Cobros sin clasificar", monthly_unclassified),
    ):
        amounts = {key: float(monthly.get(key) or 0) for key in period_keys}
        if abs(sum(amounts.values())) <= 0.5:
            continue
        for key, value in amounts.items():
            column_totals[key] = float(column_totals.get(key, 0.0) + value)
        rows.append(
            EerrConsolidatedRow(
                account_id=None,
                code="",
                name=name,
                amounts=amounts,
                total=float(sum(amounts.values())),
                row_kind=kind,
            )
        )

    grand = float(sum(monthly_vendido.values())) if any(
        abs(v) > 0.5 for v in monthly_vendido.values()
    ) else float(sum(column_totals.values()))
    return EerrConsolidatedOut(
        from_period=_period_key(start),
        to_period=_period_key(end),
        periods=period_keys,
        period_labels=period_labels,
        section_title="Ingresos",
        rows=rows,
        column_totals=column_totals,
        grand_total=grand,
        source=source,
    )
