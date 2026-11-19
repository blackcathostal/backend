from __future__ import annotations

import json
import re
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.accounting_accounts import AccountingAccounts
from app.models.customers import Customers
from app.models.suppliers import Suppliers
from app.models.tax_documents import DteStatuses, TaxDocuments
from app.models.users import Users
from app.schemas.tax_documents import (
    DteStatusOut,
    TaxDocumentClearResult,
    TaxDocumentListResult,
    TaxDocumentOut,
    TaxDocumentPatch,
    TaxDocumentRefreshResult,
)
from app.services import dte_catalog
from app.services.billing_portal_web import BillingPortalError, BillingPortalWebClient
from app.services.fns_accounting import classify_document
from app.services.fns_tax_documents import FnsTaxDocumentsClient
from app.services.fns_web_client import FnsWebError

router = APIRouter(tags=["tax-documents"])

# Placeholder customer for issued docs without a real guest/client from FNS.
GENERIC_CUSTOMER_RUT = "66.666.666-6"
GENERIC_CUSTOMER_NAME = "Sin cliente"
_GENERIC_NAME_HINTS = {"", "consumos", "consumos consumos"}

# FNS Cobros concept columns → chart of accounts code (ingresos).
_CONCEPT_ACCOUNT_CODES = (
    ("aloj", "4101"),
    ("lavan", "4130"),
    ("parking", "4103"),
    ("transfer", "4120"),
    ("tour", "4120"),
    ("bar", "4102"),
    ("caf", "4102"),
    ("frig", "4102"),
    ("desay", "4102"),
    ("deliv", "4102"),
    ("refri", "4102"),
    ("amen", "4110"),
    ("sauna", "4110"),
    ("varios", "4190"),
    ("tasas", "4190"),
)
_DEFAULT_ISSUED_ACCOUNT_CODE = "4101"


def _extract_pdf_token(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    token = str(payload.get("pdf_data") or "").strip()
    if token:
        return token
    nested = payload.get("raw")
    if isinstance(nested, dict):
        return str(nested.get("pdf_data") or "").strip()
    return ""


def _has_pdf(row: TaxDocuments) -> bool:
    raw = row.raw_json or ""
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "pdf_data" in raw
    return bool(_extract_pdf_token(payload))


def _pdf_token(row: TaxDocuments) -> str:
    raw = row.raw_json or ""
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    return _extract_pdf_token(payload)


def _resolve_type_id(db: Session, item: dict) -> int | None:
    raw = item.get("dte_type_id")
    if raw is None or raw == "":
        raw = item.get("doc_type") or item.get("dte_type_code") or ""
    # Canonical catalog only (33/34/39/61). Voucher/empty/unknown → Boleta.
    return dte_catalog.ensure_type_id(db, raw)


def _list_statuses(db: Session) -> list[DteStatusOut]:
    rows = db.query(DteStatuses).order_by(DteStatuses.id.asc()).all()
    return [DteStatusOut.model_validate(row) for row in rows]


def _supplier_name_map(db: Session, identification_numbers: list[str]) -> dict[str, str]:
    ids = sorted({str(x or "").strip() for x in identification_numbers if str(x or "").strip()})
    if not ids:
        return {}
    rows = (
        db.query(Suppliers)
        .filter(Suppliers.identification_number.in_(ids))
        .all()
    )
    return {row.identification_number: (row.company_name or "") for row in rows}


def _customer_name_map(db: Session, identification_numbers: list[str]) -> dict[str, str]:
    ids = sorted({str(x or "").strip() for x in identification_numbers if str(x or "").strip()})
    if not ids:
        return {}
    rows = (
        db.query(Customers)
        .filter(Customers.identification_number.in_(ids))
        .all()
    )
    return {row.identification_number: (row.company_name or "") for row in rows}


def _period_from_issued_at(issued_at: date | None) -> str:
    if issued_at is None:
        return ""
    return f"{issued_at.month:02d}-{issued_at.year}"


def _is_missing_customer_name(company_name: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(company_name or "").strip()).lower()
    for src, dst in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n")):
        normalized = normalized.replace(src, dst)
    return normalized in _GENERIC_NAME_HINTS


def _customer_key(identification_number: str, company_name: str) -> str:
    """Stable unique key: prefer RUT; generic customer when missing; else name key."""
    rut = str(identification_number or "").strip()
    if rut:
        return rut[:80]
    if _is_missing_customer_name(company_name):
        return GENERIC_CUSTOMER_RUT
    name = re.sub(r"\s+", " ", str(company_name or "").strip())
    if not name:
        return GENERIC_CUSTOMER_RUT
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
    return f"name:{slug or 'unknown'}"[:80]


_DTE_TYPE_LABELS_ES = {
    dte_catalog.TYPE_CODE_INVOICE: "Factura",
    dte_catalog.TYPE_CODE_EXEMPT_INVOICE: "Factura Exenta",
    dte_catalog.TYPE_CODE_RECEIPT: "Boleta",
    dte_catalog.TYPE_CODE_CREDIT_NOTE: "Nota de crédito",
}


def _dte_type_name_es(*, code: str = "", name: str = "") -> str:
    code_key = str(code or "").strip()
    if code_key in _DTE_TYPE_LABELS_ES:
        return _DTE_TYPE_LABELS_ES[code_key]
    raw = str(name or "").strip()
    low = raw.lower()
    if low in {"invoice", "factura"}:
        return "Factura"
    if low in {"receipt", "boleta", "voucher", "vale", "comprobante", ""}:
        return "Boleta"
    if "voucher" in low or "vale" in low:
        return "Boleta"
    if "exempt" in low or "exenta" in low:
        return "Factura Exenta"
    if "credit" in low or "crédito" in low or "credito" in low:
        return "Nota de crédito"
    if "boleta" in low or "receipt" in low:
        return "Boleta"
    if "factura" in low or "invoice" in low:
        return "Factura"
    # Anything else from FNS without a real SII type → show as Boleta.
    return "Boleta"


def _concept_account_code(concepts: dict | None) -> str:
    """Pick account code from the concept with the largest absolute amount."""
    rows = concepts if isinstance(concepts, dict) else {}
    best_name = ""
    best_amount = 0.0
    for name, amount in rows.items():
        value = abs(float(amount or 0))
        if value >= best_amount:
            best_amount = value
            best_name = str(name or "")
    needle = re.sub(r"\s+", " ", best_name.strip()).lower()
    for src, dst in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n")):
        needle = needle.replace(src, dst)
    for token, code in _CONCEPT_ACCOUNT_CODES:
        if token in needle:
            return code
    return _DEFAULT_ISSUED_ACCOUNT_CODE


def _resolve_accounting_account_id(db: Session, item: dict) -> int | None:
    code = str(item.get("accounting_account_code") or "").strip()
    if not code:
        concepts = item.get("concepts")
        if not isinstance(concepts, dict):
            raw = item.get("raw")
            if isinstance(raw, dict):
                concepts = raw.get("concepts")
        code = _concept_account_code(concepts if isinstance(concepts, dict) else {})
    row = (
        db.query(AccountingAccounts)
        .filter(AccountingAccounts.code == code)
        .first()
    )
    return int(row.id) if row is not None else None


def _serialize_doc(row: TaxDocuments, *, company_name: str = "") -> TaxDocumentOut:
    item = TaxDocumentOut.model_validate(row)
    item.has_pdf = _has_pdf(row)
    item.company_name = company_name or ""
    if row.dte_type is not None:
        item.dte_type_code = row.dte_type.code or ""
        item.dte_type_name = _dte_type_name_es(
            code=row.dte_type.code or "",
            name=row.dte_type.name or "",
        )
    if row.dte_version is not None:
        item.dte_version_code = row.dte_version.code or ""
        item.dte_version_name = row.dte_version.name or ""
    if row.status is not None:
        item.status_name = row.status.name or ""
        item.status_code = row.status.code or ""
        item.status_tip_color = row.status.tip_color or "red"
    if row.accounting_account is not None:
        item.accounting_account_id = row.accounting_account_id
        item.accounting_account_code = row.accounting_account.code or ""
        item.accounting_account_name = row.accounting_account.name or ""
    return item


def _parse_bound(value: str | None, *, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Fecha inválida (usa YYYY-MM-DD)") from exc


def _default_range() -> tuple[date, date]:
    from app.core.config import settings

    today = date.today()
    months = max(1, int(getattr(settings, "tax_documents_lookback_months", 12) or 12))
    year = today.year
    month = today.month - months
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1), today


def _upsert_supplier(db: Session, identification_number: str, company_name: str) -> bool:
    identification_number = str(identification_number or "").strip()
    if not identification_number:
        return False
    company_name = str(company_name or "").strip()[:220]
    # Same RUT appears many times in a Libro de Compras refresh. Pending inserts are
    # invisible to later SELECTs until flush, which caused Duplicate entry 500s.
    cache: dict[str, Suppliers] = db.info.setdefault("supplier_by_id", {})
    row = cache.get(identification_number)
    if row is None:
        row = (
            db.query(Suppliers)
            .filter(Suppliers.identification_number == identification_number)
            .first()
        )
    if row is None:
        created = False
        try:
            with db.begin_nested():
                row = Suppliers(
                    identification_number=identification_number,
                    company_name=company_name,
                )
                db.add(row)
                db.flush()
                created = True
        except IntegrityError:
            # Concurrent insert for the same RUT; keep the outer transaction.
            row = (
                db.query(Suppliers)
                .filter(Suppliers.identification_number == identification_number)
                .first()
            )
            if row is None:
                raise
            created = False
        cache[identification_number] = row
        if company_name and row.company_name != company_name:
            row.company_name = company_name
        return created
    if company_name and row.company_name != company_name:
        row.company_name = company_name
    cache[identification_number] = row
    return False


def _upsert_customer(db: Session, identification_number: str, company_name: str) -> tuple[bool, str]:
    """Upsert customer for issued docs. Returns (created, stored_identification_number)."""
    raw_name = str(company_name or "").strip()[:220]
    if _is_missing_customer_name(raw_name) and not str(identification_number or "").strip():
        key = GENERIC_CUSTOMER_RUT
        display_name = GENERIC_CUSTOMER_NAME
    else:
        key = _customer_key(identification_number, raw_name)
        display_name = raw_name or GENERIC_CUSTOMER_NAME
    if not key:
        key = GENERIC_CUSTOMER_RUT
        display_name = GENERIC_CUSTOMER_NAME
    cache: dict[str, Customers] = db.info.setdefault("customer_by_id", {})
    row = cache.get(key)
    if row is None:
        row = (
            db.query(Customers)
            .filter(Customers.identification_number == key)
            .first()
        )
    if row is None:
        created = False
        try:
            with db.begin_nested():
                row = Customers(
                    identification_number=key,
                    company_name=display_name,
                )
                db.add(row)
                db.flush()
                created = True
        except IntegrityError:
            row = (
                db.query(Customers)
                .filter(Customers.identification_number == key)
                .first()
            )
            if row is None:
                raise
            created = False
        cache[key] = row
        if display_name and row.company_name != display_name:
            row.company_name = display_name
        return created, key
    if display_name and row.company_name != display_name and key != GENERIC_CUSTOMER_RUT:
        row.company_name = display_name
    elif key == GENERIC_CUSTOMER_RUT and row.company_name != GENERIC_CUSTOMER_NAME:
        row.company_name = GENERIC_CUSTOMER_NAME
    cache[key] = row
    return False, key


def _upsert_docs(
    db: Session,
    docs: list[dict],
    *,
    source: str,
    dte_version_id: int,
    pms_rows: list[dict] | None = None,
    match_fns: bool = True,
) -> tuple[int, int, int, int]:
    upserted = 0
    classified = 0
    suppliers_upserted = 0
    customers_upserted = 0
    version_ids = dte_catalog.all_version_ids(db)
    default_status_id = dte_catalog.default_new_document_status_id(db)
    issued_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_ISSUED)
    received_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_RECEIVED)
    under_review_status_id = dte_catalog.under_review_status_id(db) or default_status_id

    for item in docs:
        version_id = int(item.get("dte_version_id") or dte_version_id or 0)
        if version_id not in version_ids:
            version_id = dte_version_id
        type_id = _resolve_type_id(db, item)
        folio = str(item.get("folio") or "").strip()
        identification_number = str(
            item.get("identification_number")
            or item.get("counterpart_rut")
            or ""
        ).strip()
        company_name = str(
            item.get("company_name") or item.get("counterpart_name") or ""
        ).strip()
        if not folio and not identification_number and not company_name:
            continue

        issued_raw = item.get("issued_at")
        issued_at = None
        if isinstance(issued_raw, date):
            issued_at = issued_raw
        elif issued_raw:
            try:
                issued_at = datetime.strptime(str(issued_raw)[:10], "%Y-%m-%d").date()
            except ValueError:
                issued_at = None
        period = _period_from_issued_at(issued_at)

        if version_id == issued_id:
            created, customer_key = _upsert_customer(
                db, identification_number, company_name
            )
            if created:
                customers_upserted += 1
            if customer_key:
                identification_number = customer_key
            if identification_number == GENERIC_CUSTOMER_RUT:
                company_name = GENERIC_CUSTOMER_NAME
        elif identification_number:
            if _upsert_supplier(db, identification_number, company_name):
                suppliers_upserted += 1
        # tax_documents.identification_number is VARCHAR(80); keep in sync with customers.
        identification_number = str(identification_number or "").strip()[:80]
        folio = str(folio or "").strip()[:40]

        # Keep classify_document compatible during transition
        classify_payload = {
            **item,
            "counterpart_rut": identification_number,
            "counterpart_name": company_name,
        }
        if match_fns:
            class_info = classify_document(classify_payload, pms_rows)
            if class_info.get("pms_class") and class_info["pms_class"] != "Sin match":
                classified += 1
        else:
            class_info = {"pms_class": "—", "pms_ref": ""}

        row = (
            db.query(TaxDocuments)
            .filter(
                TaxDocuments.dte_version_id == version_id,
                TaxDocuments.dte_type_id == type_id,
                TaxDocuments.folio == folio,
                TaxDocuments.identification_number == identification_number,
            )
            .first()
        )
        payment_status_id = dte_catalog.status_id_from_payment_code(
            db, item.get("payment_status_code")
        )
        # When FNS says paid and concepts were allocated, force Imputada Pagada.
        if (
            str(item.get("payment_status_code") or "").strip().lower() == "paid"
            and item.get("has_concepts")
        ):
            payment_status_id = dte_catalog.paid_status_id(db) or payment_status_id

        is_new = row is None
        if is_new:
            if version_id == received_id:
                initial_status_id = under_review_status_id
            else:
                initial_status_id = payment_status_id or default_status_id
            row = TaxDocuments(
                dte_version_id=version_id,
                dte_type_id=type_id,
                folio=folio,
                identification_number=identification_number,
                status_id=initial_status_id,
                period=period if version_id == issued_id else "",
            )
            db.add(row)

        row.dte_version_id = version_id
        row.dte_type_id = type_id
        # Issued refresh stamps period from document date (MM-YYYY). Recibidas: no period column.
        if version_id == issued_id and period:
            row.period = period
        elif is_new or row.period is None:
            row.period = period if version_id == issued_id else (row.period or "")
        # Issued: update paid/pending from FNS. Received: keep user classification;
        # only default En revisión when status is empty.
        if version_id == issued_id and payment_status_id:
            row.status_id = payment_status_id
        elif not row.status_id:
            row.status_id = (
                under_review_status_id
                if version_id == received_id
                else (payment_status_id or default_status_id)
            )
        row.identification_number = identification_number
        row.issued_at = issued_at
        row.net_amount = float(item.get("net_amount") or 0)
        row.exempt_amount = float(item.get("exempt_amount") or 0)
        row.tax_amount = float(item.get("tax_amount") or 0)
        row.total_amount = float(item.get("total_amount") or 0)
        row.branch = str(item.get("branch") or "")[:120]
        if item.get("pms_class"):
            row.pms_class = str(item.get("pms_class") or "Sin match")[:80]
        else:
            row.pms_class = str(class_info.get("pms_class") or "Sin match")[:80]
        if item.get("pms_ref"):
            row.pms_ref = str(item.get("pms_ref") or "")[:120]
        else:
            row.pms_ref = str(class_info.get("pms_ref") or "")[:120]
        if version_id == issued_id:
            account_id = _resolve_accounting_account_id(db, item)
            if account_id:
                row.accounting_account_id = account_id
        row.source = source
        try:
            row.raw_json = json.dumps(item.get("raw") or item, ensure_ascii=False, default=str)
        except TypeError:
            row.raw_json = None
        upserted += 1

    db.commit()
    return upserted, classified, suppliers_upserted, customers_upserted


def _list_version(
    db: Session,
    *,
    dte_version_id: int,
    date_from: date,
    date_to: date,
) -> TaxDocumentListResult:
    query = (
        db.query(TaxDocuments)
        .options(
            joinedload(TaxDocuments.status),
            joinedload(TaxDocuments.dte_type),
            joinedload(TaxDocuments.dte_version),
            joinedload(TaxDocuments.accounting_account),
        )
        .filter(TaxDocuments.dte_version_id == dte_version_id)
    )
    query = query.filter(
        TaxDocuments.issued_at.isnot(None),
        TaxDocuments.issued_at >= date_from,
        TaxDocuments.issued_at <= date_to,
    )
    rows = query.order_by(
        TaxDocuments.issued_at.desc(),
        TaxDocuments.id.desc(),
    ).all()
    issued_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_ISSUED)
    ids = [row.identification_number for row in rows]
    if dte_version_id == issued_id:
        names = _customer_name_map(db, ids)
    else:
        names = _supplier_name_map(db, ids)
    return TaxDocumentListResult(
        items=[
            _serialize_doc(row, company_name=names.get(row.identification_number or "", ""))
            for row in rows
        ],
        total=len(rows),
        from_date=date_from,
        to_date=date_to,
        statuses=_list_statuses(db),
    )


@router.get("/issued", response_model=TaxDocumentListResult)
def list_issued(
    from_date: str | None = None,
    to_date: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TaxDocumentListResult:
    start, end = _default_range()
    version_id = dte_catalog.version_id_from_raw(db, dte_catalog.VERSION_CODE_ISSUED)
    return _list_version(
        db,
        dte_version_id=version_id,
        date_from=_parse_bound(from_date, fallback=start),
        date_to=_parse_bound(to_date, fallback=end),
    )


@router.get("/received", response_model=TaxDocumentListResult)
def list_received(
    from_date: str | None = None,
    to_date: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TaxDocumentListResult:
    start, end = _default_range()
    version_id = dte_catalog.version_id_from_raw(db, dte_catalog.VERSION_CODE_RECEIVED)
    return _list_version(
        db,
        dte_version_id=version_id,
        date_from=_parse_bound(from_date, fallback=start),
        date_to=_parse_bound(to_date, fallback=end),
    )


@router.get("/emitidas", response_model=TaxDocumentListResult, include_in_schema=False)
def list_issued_legacy(
    from_date: str | None = None,
    to_date: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TaxDocumentListResult:
    return list_issued(from_date=from_date, to_date=to_date, db=db, _=_)


@router.get("/recibidas", response_model=TaxDocumentListResult, include_in_schema=False)
def list_received_legacy(
    from_date: str | None = None,
    to_date: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TaxDocumentListResult:
    return list_received(from_date=from_date, to_date=to_date, db=db, _=_)


@router.get("/statuses", response_model=list[DteStatusOut])
def list_dte_statuses(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[DteStatusOut]:
    return _list_statuses(db)


@router.patch("/documents/{doc_id}", response_model=TaxDocumentOut)
def patch_document(
    doc_id: int,
    payload: TaxDocumentPatch,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TaxDocumentOut:
    row = (
        db.query(TaxDocuments)
        .options(
            joinedload(TaxDocuments.status),
            joinedload(TaxDocuments.dte_type),
            joinedload(TaxDocuments.dte_version),
            joinedload(TaxDocuments.accounting_account),
        )
        .filter(TaxDocuments.id == doc_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    if payload.status_id is not None:
        status = db.query(DteStatuses).filter(DteStatuses.id == payload.status_id).first()
        if status is None:
            raise HTTPException(status_code=400, detail="status_id inválido")
        row.status_id = payload.status_id

    if payload.period is not None:
        row.period = str(payload.period or "").strip()[:40]

    if payload.accounting_account_id is not None:
        if payload.accounting_account_id == 0:
            row.accounting_account_id = None
        else:
            account = (
                db.query(AccountingAccounts)
                .filter(AccountingAccounts.id == payload.accounting_account_id)
                .first()
            )
            if account is None:
                raise HTTPException(status_code=400, detail="accounting_account_id inválido")
            row.accounting_account_id = payload.accounting_account_id

    db.add(row)
    db.commit()
    row = (
        db.query(TaxDocuments)
        .options(
            joinedload(TaxDocuments.status),
            joinedload(TaxDocuments.dte_type),
            joinedload(TaxDocuments.dte_version),
            joinedload(TaxDocuments.accounting_account),
        )
        .filter(TaxDocuments.id == doc_id)
        .first()
    )
    names = _supplier_name_map(db, [row.identification_number])
    return _serialize_doc(
        row,
        company_name=names.get(row.identification_number or "", ""),
    )


def _persist_pdf_token(db: Session, row: TaxDocuments, token: str) -> None:
    token = (token or "").strip()
    if not token:
        return
    payload: dict[str, object] = {}
    raw = row.raw_json or ""
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                payload = loaded
        except json.JSONDecodeError:
            payload = {}
    payload["pdf_data"] = token
    row.raw_json = json.dumps(payload, ensure_ascii=False, default=str)
    db.add(row)
    db.commit()
    db.refresh(row)


@router.get("/documents/{doc_id}/pdf")
def download_document_pdf(
    doc_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Response:
    row = db.query(TaxDocuments).filter(TaxDocuments.id == doc_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    token = _pdf_token(row)
    try:
        with BillingPortalWebClient() as web:
            if not token:
                token = web.find_pdf_data(
                    folio=row.folio or "",
                    rut=row.identification_number or "",
                    issued_at=row.issued_at,
                )
                if token:
                    _persist_pdf_token(db, row, token)
            if not token:
                raise HTTPException(
                    status_code=404,
                    detail="No encontré el PDF en facturacion.cl para este folio.",
                )
            pdf_bytes = web.download_pdf(token)
    except HTTPException:
        raise
    except BillingPortalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filename = f"DTE_{row.folio or doc_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/clear", response_model=TaxDocumentClearResult)
def clear_tax_documents(
    direction: str = "issued",
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TaxDocumentClearResult:
    """Delete all tax documents for issued or received (admin reset before re-Actualizar)."""
    try:
        version_id = dte_catalog.version_id_from_raw(db, direction)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="version must be issued/received or 1/2") from exc
    deleted = (
        db.query(TaxDocuments)
        .filter(TaxDocuments.dte_version_id == version_id)
        .delete(synchronize_session=False)
    )
    db.commit()
    return TaxDocumentClearResult(dte_version_id=version_id, deleted=int(deleted or 0))


@router.post("/refresh", response_model=TaxDocumentRefreshResult)
async def refresh_tax_documents(
    direction: str = "issued",
    from_date: str | None = None,
    to_date: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TaxDocumentRefreshResult:
    try:
        version_id = dte_catalog.version_id_from_raw(db, direction)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="version must be issued/received or 1/2") from exc

    received_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_RECEIVED)

    start, end = _default_range()
    date_from = _parse_bound(from_date, fallback=start)
    date_to = _parse_bound(to_date, fallback=end)

    warning = ""
    docs: list[dict] = []
    source = "fns_web"
    pms_rows: list[dict] = []

    try:
        if version_id == received_id:
            # Recibidas = libro de compras (facturacion.cl). FNS Contabilidad no lo expone.
            source = "web"
            with BillingPortalWebClient() as web:
                docs = web.fetch_received(date_from, date_to)
        else:
            # Emitidas = FNS Cobros + estado de cuenta / conceptos.
            source = "fns_web"
            with FnsTaxDocumentsClient() as fns:
                docs = fns.fetch_issued(date_from, date_to)
    except BillingPortalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FnsWebError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    for doc in docs:
        doc["dte_version_id"] = version_id
        resolved_type = _resolve_type_id(db, doc)
        if resolved_type is not None:
            doc["dte_type_id"] = resolved_type

    try:
        upserted, classified, suppliers_upserted, customers_upserted = _upsert_docs(
            db,
            docs,
            source=source,
            dte_version_id=version_id,
            pms_rows=pms_rows,
            match_fns=False,
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail=(
                "No se pudieron guardar los documentos (conflicto de proveedores/DTE). "
                "Reintentá Actualizar."
            ),
        ) from exc
    except Exception as exc:
        # Surface DataError (e.g. column too long) as 400 instead of opaque 500.
        db.rollback()
        message = str(exc)
        if "Data too long" in message or "1406" in message:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Un RUT/identificador de cliente es demasiado largo para guardar. "
                    "Actualizá el API (git pull + restart) si sigue pasando."
                ),
            ) from exc
        raise
    return TaxDocumentRefreshResult(
        dte_version_id=version_id,
        fetched=len(docs),
        upserted=upserted,
        classified=classified,
        suppliers_upserted=suppliers_upserted,
        customers_upserted=customers_upserted,
        warning=warning,
    )
