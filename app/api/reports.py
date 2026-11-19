"""Accounting reports: EERR (income statement) and consolidated by month."""

from __future__ import annotations

import logging
import re
from calendar import monthrange
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.accounting_accounts import AccountingAccounts
from app.models.customers import Customers
from app.models.hr_period_amounts import HrPeriodAmounts
from app.models.suppliers import Suppliers
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
    account_code_for_concept,
    ensure_concept_labels_from_coa,
    load_expense_accounts,
    load_income_accounts,
)
from app.api.tax_documents import _has_pdf
from app.services.extranet_web_client import ExtranetWebError
from app.services.tax_documents_fetch import TaxDocumentsFetchClient, _split_cl_iva

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
    """Canonical period key for UI/API: MM-YYYY (same as issued Track)."""
    return f"{day.month:02d}-{day.year:04d}"


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
    *,
    extra_by_account_id: dict[int, float] | None = None,
) -> list[EerrAccountRow]:
    extras = extra_by_account_id or {}
    return [
        EerrAccountRow(
            account_id=int(account.id),
            code=account.code or "",
            name=account.name or "",
            total=float(by_code.get(str(account.code or ""), 0.0))
            + float(extras.get(int(account.id), 0.0)),
            row_kind="account",
            category=str(account.category or ""),
        )
        for account in accounts
    ]


def _hr_amounts_by_account_id(db: Session, period_key: str) -> dict[int, float]:
    """Monthly Remuneraciones amounts keyed by accounting_account_id."""
    return {
        int(row.accounting_account_id): float(row.amount or 0)
        for row in db.query(HrPeriodAmounts)
        .filter(HrPeriodAmounts.period == period_key)
        .all()
    }


def _received_totals_by_account_id(
    db: Session,
    *,
    date_from: date,
    date_to: date,
) -> dict[int, float]:
    """Σ recibidas (compras) by assigned accounting account for the month."""
    received_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_RECEIVED)
    if received_id is None:
        return {}
    rows = (
        db.query(
            TaxDocuments.accounting_account_id,
            func.coalesce(func.sum(TaxDocuments.total_amount), 0.0),
        )
        .filter(
            TaxDocuments.dte_version_id == int(received_id),
            TaxDocuments.accounting_account_id.isnot(None),
            TaxDocuments.issued_at.isnot(None),
            TaxDocuments.issued_at >= date_from,
            TaxDocuments.issued_at <= date_to,
        )
        .group_by(TaxDocuments.accounting_account_id)
        .all()
    )
    return {
        int(account_id): float(total or 0)
        for account_id, total in rows
        if account_id is not None
    }


def _commission_fees_by_account_id(
    db: Session,
    *,
    date_from: date,
    date_to: date,
    accounts: list[AccountingAccounts],
) -> dict[int, float]:
    """
    Map FNS Informes/Comisiones channel fees (Booking, Expedia, …) to gasto accounts
    via accounting_accounts.concept_labels.
    """
    if not accounts:
        return {}
    try:
        client = TaxDocumentsFetchClient()
        by_channel = client.fetch_commission_fees_by_channel(date_from, date_to)
    except ExtranetWebError as exc:
        logger.warning("EERR commissions unavailable: %s", exc)
        return {}
    code_to_id = {
        str(account.code or "").strip(): int(account.id)
        for account in accounts
        if str(account.code or "").strip()
    }
    totals: dict[int, float] = {}
    for channel, fee in by_channel.items():
        # Do not fall through to income catch-all (*) for OTA channel names.
        code = account_code_for_concept(channel, accounts, allow_catch_all=False)
        if not code:
            continue
        account_id = code_to_id.get(str(code))
        if account_id is None:
            continue
        totals[account_id] = float(totals.get(account_id, 0.0) + float(fee or 0))
    return totals


def _expense_rows_for_period(
    db: Session,
    *,
    period_key: str,
    date_from: date,
    date_to: date,
) -> list[EerrAccountRow]:
    """
    Egresos: all active gasto accounts.
    Remuneraciones ← hr_period_amounts;
    OTA commissions ← FNS Informes/Comisiones (Booking/Expedia/…);
    other categories ← recibidas assigned totals.
    """
    accounts = load_expense_accounts(db)
    if not accounts:
        return []
    hr_amounts = _hr_amounts_by_account_id(db, period_key)
    received_amounts = _received_totals_by_account_id(
        db, date_from=date_from, date_to=date_to
    )
    commission_amounts = _commission_fees_by_account_id(
        db, date_from=date_from, date_to=date_to, accounts=accounts
    )
    rows: list[EerrAccountRow] = []
    for account in accounts:
        category = str(account.category or "").strip()
        account_id = int(account.id)
        if category.lower() == "remuneraciones":
            amount = float(hr_amounts.get(account_id, 0.0))
        elif account_id in commission_amounts and abs(
            float(commission_amounts.get(account_id, 0.0))
        ) > 0.5:
            # Prefer FNS channel commission when present; keep recibidas if higher.
            amount = max(
                float(commission_amounts.get(account_id, 0.0)),
                float(received_amounts.get(account_id, 0.0)),
            )
        else:
            amount = float(received_amounts.get(account_id, 0.0))
        rows.append(
            EerrAccountRow(
                account_id=account_id,
                code=str(account.code or ""),
                name=str(account.name or ""),
                total=amount,
                row_kind="expense",
                category=category,
            )
        )
    return rows


def _issued_doc_signed_amount(db: Session, *, type_id: int | None, amount: float) -> float:
    """Apply NC sign so credit notes reduce issued income totals."""
    value = float(amount or 0)
    if abs(value) <= 0.5:
        return 0.0
    credit_id = dte_catalog.type_id_by_code(db, dte_catalog.TYPE_CODE_CREDIT_NOTE)
    if credit_id is not None and type_id is not None and int(type_id) == int(credit_id):
        return -abs(value)
    return value


def _issued_totals_by_account_id(
    db: Session,
    *,
    date_from: date,
    date_to: date,
    exclude_invoice_types: bool = False,
) -> dict[int, float]:
    """
    Σ issued docs by assigned accounting account for the month.

    When exclude_invoice_types=True, skip 33/34/61 so EERR income rows use
    income-collections lines and Facturado stays a separate KPI (no double count).
    """
    issued_id = _issued_version_id(db)
    invoice_type_ids = set(_invoice_type_ids(db)) if exclude_invoice_types else set()
    rows = (
        db.query(
            TaxDocuments.accounting_account_id,
            TaxDocuments.dte_type_id,
            TaxDocuments.total_amount,
        )
        .filter(
            TaxDocuments.dte_version_id == issued_id,
            TaxDocuments.accounting_account_id.isnot(None),
            TaxDocuments.issued_at.isnot(None),
            TaxDocuments.issued_at >= date_from,
            TaxDocuments.issued_at <= date_to,
        )
        .all()
    )
    totals: dict[int, float] = {}
    for account_id, type_id, amount in rows:
        if account_id is None:
            continue
        if invoice_type_ids and type_id is not None and int(type_id) in invoice_type_ids:
            continue
        value = _issued_doc_signed_amount(db, type_id=type_id, amount=float(amount or 0))
        if abs(value) <= 0.5:
            continue
        key = int(account_id)
        totals[key] = float(totals.get(key, 0.0) + value)
    return totals


def _invoice_totals_by_account_id(
    db: Session,
    *,
    date_from: date,
    date_to: date,
) -> dict[int, float]:
    """Σ facturas/NC emitidas (33/34/61) grouped by assigned accounting account."""
    issued_id = _issued_version_id(db)
    type_ids = _invoice_type_ids(db)
    if not type_ids:
        return {}
    rows = (
        db.query(
            TaxDocuments.accounting_account_id,
            TaxDocuments.dte_type_id,
            TaxDocuments.total_amount,
        )
        .filter(
            TaxDocuments.dte_version_id == issued_id,
            TaxDocuments.dte_type_id.in_(type_ids),
            TaxDocuments.accounting_account_id.isnot(None),
            TaxDocuments.issued_at.isnot(None),
            TaxDocuments.issued_at >= date_from,
            TaxDocuments.issued_at <= date_to,
        )
        .all()
    )
    totals: dict[int, float] = {}
    for account_id, type_id, amount in rows:
        if account_id is None:
            continue
        value = _issued_doc_signed_amount(db, type_id=type_id, amount=float(amount or 0))
        if abs(value) <= 0.5:
            continue
        key = int(account_id)
        totals[key] = float(totals.get(key, 0.0) + value)
    return totals


def _unassigned_issued_total(
    db: Session,
    *,
    date_from: date,
    date_to: date,
    exclude_invoice_types: bool = False,
) -> float:
    """Σ issued docs still without accounting_account_id (NC negative)."""
    issued_id = _issued_version_id(db)
    invoice_type_ids = set(_invoice_type_ids(db)) if exclude_invoice_types else set()
    rows = (
        db.query(TaxDocuments.dte_type_id, TaxDocuments.total_amount)
        .filter(
            TaxDocuments.dte_version_id == issued_id,
            TaxDocuments.accounting_account_id.is_(None),
            TaxDocuments.issued_at.isnot(None),
            TaxDocuments.issued_at >= date_from,
            TaxDocuments.issued_at <= date_to,
        )
        .all()
    )
    total = 0.0
    for type_id, amount in rows:
        if invoice_type_ids and type_id is not None and int(type_id) in invoice_type_ids:
            continue
        total += _issued_doc_signed_amount(db, type_id=type_id, amount=float(amount or 0))
    return float(total)


def _income_rows_from_issued(
    accounts: list[AccountingAccounts],
    issued_by_account_id: dict[int, float],
    *,
    unassigned_total: float = 0.0,
) -> list[EerrAccountRow]:
    """Build income rows from issued assigned totals (mirror of received expenses)."""
    rows: list[EerrAccountRow] = [
        EerrAccountRow(
            account_id=int(account.id),
            code=str(account.code or ""),
            name=str(account.name or ""),
            total=float(issued_by_account_id.get(int(account.id), 0.0)),
            row_kind="account",
            category=str(account.category or ""),
        )
        for account in accounts
    ]
    if abs(unassigned_total) > 0.5:
        rows.append(
            EerrAccountRow(
                account_id=None,
                code="",
                name="Emitidas sin clasificar",
                total=float(unassigned_total),
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


def _invoice_type_ids(db: Session) -> list[int]:
    ids: list[int] = []
    for code in (
        dte_catalog.TYPE_CODE_INVOICE,
        dte_catalog.TYPE_CODE_EXEMPT_INVOICE,
        dte_catalog.TYPE_CODE_CREDIT_NOTE,
    ):
        type_id = dte_catalog.type_id_by_code(db, code)
        if type_id is not None:
            ids.append(int(type_id))
    return ids


def _customer_names_for(db: Session, identification_numbers: list[str]) -> dict[str, str]:
    ids = sorted({str(x or "").strip() for x in identification_numbers if str(x or "").strip()})
    if not ids:
        return {}
    rows = (
        db.query(Customers)
        .filter(Customers.identification_number.in_(ids))
        .all()
    )
    return {
        str(row.identification_number or ""): str(row.company_name or "")
        for row in rows
    }


def _supplier_names_for(db: Session, identification_numbers: list[str]) -> dict[str, str]:
    ids = sorted({str(x or "").strip() for x in identification_numbers if str(x or "").strip()})
    if not ids:
        return {}
    rows = (
        db.query(Suppliers)
        .filter(Suppliers.identification_number.in_(ids))
        .all()
    )
    return {
        str(row.identification_number or ""): str(row.company_name or "")
        for row in rows
    }


def _received_tax_document_lines(
    db: Session,
    *,
    date_from: date,
    date_to: date,
    account_id: int | None = None,
) -> list[dict]:
    """Detail lines from received tax_documents (compras) for an expense account."""
    received_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_RECEIVED)
    if received_id is None:
        return []
    query = (
        db.query(TaxDocuments)
        .options(joinedload(TaxDocuments.dte_type))
        .filter(
            TaxDocuments.dte_version_id == int(received_id),
            TaxDocuments.issued_at.isnot(None),
            TaxDocuments.issued_at >= date_from,
            TaxDocuments.issued_at <= date_to,
        )
    )
    if account_id is not None:
        query = query.filter(TaxDocuments.accounting_account_id == int(account_id))

    rows = query.order_by(TaxDocuments.issued_at.asc(), TaxDocuments.id.asc()).all()
    names = _supplier_names_for(db, [row.identification_number for row in rows])
    lines: list[dict] = []
    for row in rows:
        amount = float(row.total_amount or 0)
        if abs(amount) <= 0.5:
            continue
        type_code = ""
        type_name = ""
        if row.dte_type is not None:
            type_code = str(row.dte_type.code or "").strip()
            type_name = str(row.dte_type.name or type_code).strip()
        if type_code == dte_catalog.TYPE_CODE_CREDIT_NOTE:
            amount = -abs(amount)
        folio = str(row.folio or "").strip()
        lines.append(
            {
                "issued_at": row.issued_at.isoformat() if row.issued_at else None,
                "folio": folio[:40],
                "doc_type": type_name or "DTE",
                "company_name": names.get(str(row.identification_number or ""), "")[:220],
                "locator": str(row.pms_ref or "")[:120],
                "concept": type_name or "Documento tributario",
                "amount": amount,
                "tax_document_id": int(row.id),
                "has_pdf": bool(_has_pdf(row)),
            }
        )
    return lines


def _commission_detail_lines_for_account(
    *,
    date_from: date,
    date_to: date,
    accounts: list[AccountingAccounts],
    account_id: int,
) -> list[dict]:
    """FNS channel commission fees that map to the given expense account."""
    try:
        client = TaxDocumentsFetchClient()
        by_channel = client.fetch_commission_fees_by_channel(date_from, date_to)
    except ExtranetWebError as exc:
        logger.warning("EERR commission detail unavailable: %s", exc)
        return []
    code_to_id = {
        str(account.code or "").strip(): int(account.id)
        for account in accounts
        if str(account.code or "").strip()
    }
    lines: list[dict] = []
    for channel, fee in by_channel.items():
        amount = float(fee or 0)
        if abs(amount) <= 0.5:
            continue
        code = account_code_for_concept(channel, accounts, allow_catch_all=False)
        if not code:
            continue
        if code_to_id.get(str(code)) != int(account_id):
            continue
        lines.append(
            {
                "issued_at": None,
                "folio": "",
                "doc_type": "Comisión",
                "company_name": str(channel or "")[:220],
                "locator": "",
                "concept": f"Comisión {channel}".strip(),
                "amount": amount,
            }
        )
    return lines


def _issued_tax_document_lines(
    db: Session,
    *,
    date_from: date,
    date_to: date,
    account_id: int | None = None,
    invoice_only: bool = False,
    unassigned_only: bool = False,
) -> list[dict]:
    """Detail lines from issued tax_documents (includes facturas from facturacion.cl)."""
    issued_id = _issued_version_id(db)
    query = (
        db.query(TaxDocuments)
        .options(joinedload(TaxDocuments.dte_type))
        .filter(
            TaxDocuments.dte_version_id == issued_id,
            TaxDocuments.issued_at.isnot(None),
            TaxDocuments.issued_at >= date_from,
            TaxDocuments.issued_at <= date_to,
        )
    )
    if unassigned_only:
        query = query.filter(TaxDocuments.accounting_account_id.is_(None))
    elif account_id is not None:
        query = query.filter(TaxDocuments.accounting_account_id == int(account_id))
    if invoice_only:
        type_ids = _invoice_type_ids(db)
        if not type_ids:
            return []
        query = query.filter(TaxDocuments.dte_type_id.in_(type_ids))

    rows = query.order_by(TaxDocuments.issued_at.asc(), TaxDocuments.id.asc()).all()
    names = _customer_names_for(db, [row.identification_number for row in rows])
    lines: list[dict] = []
    for row in rows:
        amount = float(row.total_amount or 0)
        if abs(amount) <= 0.5:
            continue
        type_code = ""
        type_name = ""
        if row.dte_type is not None:
            type_code = str(row.dte_type.code or "").strip()
            type_name = str(row.dte_type.name or type_code).strip()
        # Portal/FNS often store NC (61) as positive; Facturado must net them out.
        if type_code == dte_catalog.TYPE_CODE_CREDIT_NOTE:
            amount = -abs(amount)
        folio = str(row.folio or "").strip()
        lines.append(
            {
                "issued_at": row.issued_at.isoformat() if row.issued_at else None,
                "folio": folio[:40],
                "doc_type": type_name or "DTE",
                "company_name": names.get(str(row.identification_number or ""), "")[:220],
                "locator": str(row.pms_ref or "")[:120],
                "concept": type_name or "Documento tributario",
                "amount": amount,
                "tax_document_id": int(row.id),
                "has_pdf": bool(_has_pdf(row)),
            }
        )
    return lines


def _merge_detail_lines(
    primary: list[dict],
    extra: list[dict],
) -> list[dict]:
    """Append extra lines; skip extras whose folio already appears in primary."""
    seen_folios: set[str] = set()
    merged: list[dict] = []
    for item in primary:
        folio = re.sub(r"\s+", "", str(item.get("folio") or ""))
        if folio:
            seen_folios.add(folio)
        merged.append(item)
    for item in extra:
        folio = re.sub(r"\s+", "", str(item.get("folio") or ""))
        if folio and folio in seen_folios:
            continue
        if folio:
            seen_folios.add(folio)
        merged.append(item)
    return merged


def _fetch_extranet_month(date_from: date, date_to: date, *, accounts: list) -> dict:
    client = TaxDocumentsFetchClient()
    return client.fetch_income_by_account(date_from, date_to, accounts=accounts)


@router.get("/eerr", response_model=EerrReportOut)
def get_eerr_report(
    period: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> EerrReportOut:
    """
    Income statement for one month.

    Income ← issued tax documents by account (same idea as received docs for expenses).
    Expenses ← received docs + HR + OTA commissions (FNS commissions report).
    Collected/pending KPIs still come from FNS income-collections when available.
    """
    month_start = _parse_period(period)
    # Cap "this month" to today so mid-month matches FNS open range.
    today = date.today()
    month_finish = min(_month_end(month_start), today) if (
        month_start.year == today.year and month_start.month == today.month
    ) else _month_end(month_start)
    ensure_concept_labels_from_coa(db)
    accounts = load_income_accounts(db)

    source = "tax_documents_issued"
    # Income rows: income-collections / boletas — not facturacion.cl invoices (Facturado KPI).
    issued_by_account = _issued_totals_by_account_id(
        db,
        date_from=month_start,
        date_to=month_finish,
        exclude_invoice_types=True,
    )
    unassigned_total = _unassigned_issued_total(
        db,
        date_from=month_start,
        date_to=month_finish,
        exclude_invoice_types=True,
    )
    rows = _income_rows_from_issued(
        accounts,
        issued_by_account,
        unassigned_total=unassigned_total,
    )
    grand_total = float(sum(float(row.total or 0) for row in rows))
    concepts_total = float(sum(float(v or 0) for v in issued_by_account.values()))
    unclassified_collections = float(unassigned_total)
    pending_sales = 0.0
    sold = float(grand_total)
    collected = 0.0
    pending = 0.0

    # Cash KPIs from FNS income-collections (sold/collected/pending).
    try:
        sales = TaxDocumentsFetchClient()._income_collections_sales_totals(
            month_start, month_finish
        )
        sold = float(sales.get("sold") or 0) or sold
        collected = float(sales.get("collected") or 0)
        pending = float(sales.get("pending") or 0)
    except ExtranetWebError as exc:
        logger.warning("EERR collected/pending unavailable: %s", exc)

    # Invoiced KPI: invoices/credit notes 33/34/61 (assigned + unassigned).
    invoice_by_account = _invoice_totals_by_account_id(
        db, date_from=month_start, date_to=month_finish
    )
    invoiced = float(sum(invoice_by_account.values()))
    unassigned_invoice_lines = _issued_tax_document_lines(
        db,
        date_from=month_start,
        date_to=month_finish,
        invoice_only=True,
        unassigned_only=True,
    )
    invoiced += float(
        sum(float(item.get("amount") or 0) for item in unassigned_invoice_lines)
    )

    grand_net, _grand_exempt, grand_tax = _split_cl_iva(grand_total)
    period_key = _period_key(month_start)
    expense_rows = _expense_rows_for_period(
        db,
        period_key=period_key,
        date_from=month_start,
        date_to=month_finish,
    )
    expenses_total = float(sum(float(row.total or 0) for row in expense_rows))
    result_total = float(grand_total - expenses_total)
    return EerrReportOut(
        period=period_key,
        section_title="Ingresos",
        rows=rows,
        grand_total=grand_total,
        grand_net=grand_net,
        grand_tax=grand_tax,
        expense_section_title="Egresos",
        expense_rows=expense_rows,
        expenses_total=expenses_total,
        result_total=result_total,
        sold=sold,
        collected=collected,
        pending=pending,
        invoiced=invoiced,
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
    """Drill-down lines for an EERR account or remainder row."""
    month_start = _parse_period(period)
    today = date.today()
    month_finish = min(_month_end(month_start), today) if (
        month_start.year == today.year and month_start.month == today.month
    ) else _month_end(month_start)

    ensure_concept_labels_from_coa(db)
    income_accounts = load_income_accounts(db)
    expense_accounts = load_expense_accounts(db)
    kind = str(row_kind or "account").strip().lower()
    code = str(account_code or "").strip()
    account_name = ""
    resolved_id = account_id
    expense_category = ""

    if kind == "account":
        account = None
        if account_id is not None:
            account = next(
                (row for row in income_accounts if int(row.id) == int(account_id)),
                None,
            )
        if account is None and code:
            account = next(
                (row for row in income_accounts if str(row.code or "") == code),
                None,
            )
        if account is None:
            raise HTTPException(status_code=404, detail="Cuenta contable no encontrada")
        code = str(account.code or "")
        account_name = str(account.name or "")
        resolved_id = int(account.id)
        title = f"{code} · {account_name}".strip(" ·")
    elif kind == "expense":
        account = None
        if account_id is not None:
            account = next(
                (row for row in expense_accounts if int(row.id) == int(account_id)),
                None,
            )
        if account is None and code:
            account = next(
                (row for row in expense_accounts if str(row.code or "") == code),
                None,
            )
        if account is None:
            raise HTTPException(status_code=404, detail="Cuenta de egreso no encontrada")
        code = str(account.code or "")
        account_name = str(account.name or "")
        resolved_id = int(account.id)
        expense_category = str(account.category or "").strip()
        title = f"{code} · {account_name}".strip(" ·")
    elif kind == "pending":
        title = "Pendiente por cobrar"
    elif kind == "collected":
        title = "Cobrado"
    elif kind == "unclassified":
        title = "Emitidas sin clasificar"
    elif kind in {"invoiced", "facturado"}:
        kind = "invoiced"
        title = "Facturado"
    else:
        raise HTTPException(status_code=400, detail="row_kind inválido")

    raw_lines: list[dict] = []
    if kind == "expense":
        if expense_category.lower() == "remuneraciones" and resolved_id is not None:
            hr_amount = float(
                _hr_amounts_by_account_id(db, _period_key(month_start)).get(
                    int(resolved_id), 0.0
                )
            )
            if abs(hr_amount) > 0.5:
                raw_lines = [
                    {
                        "issued_at": None,
                        "folio": "",
                        "doc_type": "RRHH",
                        "company_name": "",
                        "locator": "",
                        "concept": "Remuneraciones del periodo",
                        "amount": hr_amount,
                    }
                ]
        else:
            raw_lines = _received_tax_document_lines(
                db,
                date_from=month_start,
                date_to=month_finish,
                account_id=int(resolved_id) if resolved_id is not None else None,
            )
            if resolved_id is not None:
                commission_lines = _commission_detail_lines_for_account(
                    date_from=month_start,
                    date_to=month_finish,
                    accounts=expense_accounts,
                    account_id=int(resolved_id),
                )
                if commission_lines and not raw_lines:
                    raw_lines = commission_lines
                elif commission_lines and raw_lines:
                    # Prefer document lines; keep commissions only when they exceed docs.
                    docs_total = float(sum(float(item.get("amount") or 0) for item in raw_lines))
                    fees_total = float(
                        sum(float(item.get("amount") or 0) for item in commission_lines)
                    )
                    if fees_total > docs_total + 0.5:
                        raw_lines = commission_lines
    elif kind == "invoiced":
        raw_lines = _issued_tax_document_lines(
            db,
            date_from=month_start,
            date_to=month_finish,
            invoice_only=True,
        )
    elif kind == "account":
        raw_lines = _issued_tax_document_lines(
            db,
            date_from=month_start,
            date_to=month_finish,
            account_id=int(resolved_id) if resolved_id is not None else None,
            invoice_only=False,
        )
    elif kind == "unclassified":
        raw_lines = _issued_tax_document_lines(
            db,
            date_from=month_start,
            date_to=month_finish,
            invoice_only=False,
            unassigned_only=True,
        )
    else:
        # pending / collected: still from FNS income-collections
        try:
            client = TaxDocumentsFetchClient()
            raw_lines = client.fetch_income_detail_lines(
                month_start,
                month_finish,
                accounts=income_accounts,
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
    """Income statement by account with one total column per month (issued docs)."""
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
    monthly_unclassified: dict[str, float] = {key: 0.0 for key in period_keys}
    source = "tax_documents_issued"

    for month in months:
        key = _period_key(month)
        finish = min(_month_end(month), today) if (
            month.year == today.year and month.month == today.month
        ) else _month_end(month)
        issued_by_account = _issued_totals_by_account_id(
            db,
            date_from=month,
            date_to=finish,
            exclude_invoice_types=True,
        )
        code_by_id = {int(account.id): str(account.code or "") for account in accounts}
        by_code: dict[str, float] = {}
        for account_id, amount in issued_by_account.items():
            code = code_by_id.get(int(account_id), "")
            if not code:
                continue
            by_code[code] = float(by_code.get(code, 0.0) + float(amount or 0))
        monthly_by_code[key] = by_code
        monthly_unclassified[key] = float(
            _unassigned_issued_total(
                db,
                date_from=month,
                date_to=finish,
                exclude_invoice_types=True,
            )
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

    unclassified_amounts = {
        key: float(monthly_unclassified.get(key) or 0) for key in period_keys
    }
    if abs(sum(unclassified_amounts.values())) > 0.5:
        for key, value in unclassified_amounts.items():
            column_totals[key] = float(column_totals.get(key, 0.0) + value)
        rows.append(
            EerrConsolidatedRow(
                account_id=None,
                code="",
                name="Emitidas sin clasificar",
                amounts=unclassified_amounts,
                total=float(sum(unclassified_amounts.values())),
                row_kind="unclassified",
            )
        )

    grand = float(sum(column_totals.values()))
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
