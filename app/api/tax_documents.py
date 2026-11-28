from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.accounting_accounts import AccountingAccounts
from app.models.customers import Customers
from app.models.suppliers import Suppliers
from app.models.tax_documents import DteStatuses, DteTypes, TaxDocuments
from app.models.users import Users
from app.schemas.tax_documents import (
    DteStatusOut,
    TaxDocumentBulkPaid,
    TaxDocumentClearResult,
    TaxDocumentCreate,
    TaxDocumentListResult,
    TaxDocumentOut,
    TaxDocumentPatch,
    TaxDocumentRefreshResult,
)
from app.services import dte_catalog
from app.services.accounting_concept_map import (
    ensure_concept_labels_from_coa,
    load_income_accounts,
    lodging_income_account,
    pick_account_from_concepts,
)
from app.services.billing_portal_client import BillingPortalClient
from app.services.billing_portal_web import BillingPortalError, BillingPortalWebClient
from app.services.accounting_classify import classify_document
from app.services.tax_documents_fetch import TaxDocumentsFetchClient, _split_cl_iva
from app.services.extranet_web_client import ExtranetWebError
from app.services.manual_received_boletas import ensure_manual_received_boletas
from app.services.received_document_account_map import (
    ensure_received_document_account_mappings_from_catalog,
    load_active_received_account_map,
    resolve_received_accounting_account_id,
)
from app.services.utility_bills import (
    fetch_utility_received_documents,
    load_utility_bill_accounts,
)

# Issued DTE type codes treated as invoices (import from facturacion.cl when missing in FNS).
_INVOICE_TYPE_CODES = frozenset({"33", "34", "61"})
# Boletas stay FNS-only; facturacion.cl is used to corroborate.
_BOLETA_TYPE_CODES = frozenset({"39", "41"})


def _issued_doc_key(doc: dict) -> tuple[str, str]:
    return (
        str(doc.get("dte_type_code") or doc.get("dte_type_id") or "").strip(),
        re.sub(r"\s+", "", str(doc.get("folio") or "")),
    )


def _folio_match_key(value: object) -> str:
    """Normalize folio/Nº documento so '500', '0500' and 'N° 500' compare equal."""
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return ""
    return digits.lstrip("0") or digits


def _normalize_header_key(value: object) -> str:
    text = str(value or "").strip().lower()
    for src, dst in (
        ("á", "a"),
        ("é", "e"),
        ("í", "i"),
        ("ó", "o"),
        ("ú", "u"),
        ("ñ", "n"),
        ("º", ""),
        ("°", ""),
        ("ª", ""),
    ):
        text = text.replace(src, dst)
    return re.sub(r"[^a-z0-9]+", "", text)


def _raw_document_number(raw: dict) -> str:
    for key, value in raw.items():
        normalized = _normalize_header_key(key)
        if normalized in {
            "ndocumento",
            "nrodocumento",
            "nodocumento",
            "numerodocumento",
            "folio",
        }:
            text = str(value or "").strip()
            if text:
                return text
        if "documento" in normalized and "tipo" not in normalized:
            text = str(value or "").strip()
            if text:
                return text
    return ""


def _fns_referenced_invoice_folio(doc: dict) -> str:
    """
    Invoice number already carried by an FNS Cobros row.

    Cobros keeps payment ID as folio and puts the real factura number in
    "Nº documento" / comments ("Pagado por factura Nro. 500"). Those must
    block a second copy from facturacion.cl.
    """
    raw = doc.get("raw") if isinstance(doc.get("raw"), dict) else {}
    doc_type = str(
        doc.get("doc_type")
        or doc.get("dte_type_code")
        or raw.get("doc_type")
        or raw.get("Tipo documento")
        or ""
    )
    comment = str(raw.get("Comentarios") or raw.get("comentarios") or "")
    code = str(doc.get("dte_type_code") or doc.get("dte_type_id") or "").strip()
    lowered_type = doc_type.lower()
    lowered_comment = comment.lower()
    is_invoice_like = (
        code in _INVOICE_TYPE_CODES
        or "factura" in lowered_type
        or "factura" in lowered_comment
        or "nota de credito" in lowered_type
        or "nota de crédito" in lowered_type
    )
    if not is_invoice_like:
        return ""
    digits = _folio_match_key(_raw_document_number(raw))
    if digits:
        return digits
    match = re.search(
        r"factura\s*(?:nro\.?|n[oº°\.]*|numero)?\s*(\d+)",
        comment,
        flags=re.I,
    )
    if match:
        return _folio_match_key(match.group(1))
    return ""


def _is_placeholder_identification(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    lowered = text.lower()
    return lowered.startswith("name:") or lowered in {
        "66.666.666-6",
        "66666666-6",
    }


def _is_real_rut(value: object) -> bool:
    text = str(value or "").strip()
    if not text or _is_placeholder_identification(text):
        return False
    return bool(re.fullmatch(r"\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]", text, flags=re.I)) or bool(
        re.fullmatch(r"\d{7,8}-[\dkK]", text, flags=re.I)
    )


def _merge_issued_with_billing_portal(
    fns_docs: list[dict],
    portal_docs: list[dict],
) -> tuple[list[dict], str]:
    """
    Keep all FNS emitidas. Import portal invoices missing in FNS.
    If FNS Cobros already references a factura Nº, skip that folio from
    facturacion.cl (avoids paying-with-invoice duplicates).
    Enrich FNS rows that lack RUT with facturacion.cl invoice RUT/name.
    Use portal boletas only to corroborate FNS boletas (do not import portal-only boletas).
    """
    portal_boleta_folios: set[str] = set()
    portal_invoices_by_folio: dict[str, dict] = {}
    portal_invoice_keys: set[tuple[str, str]] = set()

    for doc in portal_docs:
        key = _issued_doc_key(doc)
        code = key[0]
        folio = key[1]
        if not folio:
            continue
        raw_obj = doc.get("raw") if isinstance(doc.get("raw"), dict) else {}
        tipo = str(
            doc.get("doc_type")
            or raw_obj.get("tipo")
            or raw_obj.get("DOCUMENTO")
            or ""
        ).lower()
        if code in _BOLETA_TYPE_CODES or "boleta" in tipo:
            portal_boleta_folios.add(folio)
            continue
        is_invoice = code in _INVOICE_TYPE_CODES or (
            "factura" in tipo
            or "nota de credito" in tipo
            or "nota de crédito" in tipo
        )
        if not is_invoice:
            continue
        portal_invoice_keys.add(key)
        # Prefer rows that carry a real RUT when the same folio appears twice.
        current = portal_invoices_by_folio.get(folio)
        if current is None or (
            _is_real_rut(doc.get("identification_number"))
            and not _is_real_rut(current.get("identification_number"))
        ):
            portal_invoices_by_folio[folio] = doc

    portal_by_folio_key = {
        _folio_match_key(folio): doc
        for folio, doc in portal_invoices_by_folio.items()
        if _folio_match_key(folio)
    }

    merged: list[dict] = []
    fns_keys: set[tuple[str, str]] = set()
    fns_boleta_folios: set[str] = set()
    fns_referenced_invoice_folios: set[str] = set()
    enriched_ruts = 0

    for doc in fns_docs:
        item = dict(doc)
        item.setdefault("source", "extranet_web")
        key = _issued_doc_key(item)
        folio = key[1]
        code = key[0]
        if folio:
            fns_keys.add(key)
        if code in _BOLETA_TYPE_CODES or (
            not code and "boleta" in str(item.get("doc_type") or "").lower()
        ):
            if folio:
                fns_boleta_folios.add(folio)

        referenced_folio = _fns_referenced_invoice_folio(item)
        if referenced_folio:
            fns_referenced_invoice_folios.add(referenced_folio)

        portal = portal_invoices_by_folio.get(folio) if folio else None
        if portal is None and referenced_folio:
            portal = portal_by_folio_key.get(referenced_folio)
        fns_tipo = str(item.get("doc_type") or "").lower()
        fns_is_invoice = (
            code in _INVOICE_TYPE_CODES
            or "factura" in fns_tipo
            or bool(referenced_folio)
        )
        if (
            portal
            and _is_placeholder_identification(item.get("identification_number"))
            and (fns_is_invoice or not code)
        ):
            portal_rut = str(
                portal.get("identification_number") or portal.get("counterpart_rut") or ""
            ).strip()
            portal_name = str(
                portal.get("company_name") or portal.get("counterpart_name") or ""
            ).strip()
            if _is_real_rut(portal_rut):
                item["identification_number"] = portal_rut
                item["counterpart_rut"] = portal_rut
                if portal_name:
                    item["company_name"] = portal_name
                    item["counterpart_name"] = portal_name
                raw = item.get("raw")
                if isinstance(raw, dict):
                    raw = dict(raw)
                    raw["billing_portal_rut_enriched"] = True
                    item["raw"] = raw
                enriched_ruts += 1
        # Prefer portal totals/neto when FNS payment row has no usable amount.
        if portal and float(item.get("total_amount") or 0) <= 0.5:
            for field in ("total_amount", "net_amount", "tax_amount", "exempt_amount"):
                portal_amount = float(portal.get(field) or 0)
                if portal_amount:
                    item[field] = portal_amount
        elif portal and float(item.get("net_amount") or 0) <= 0.5:
            for field in ("net_amount", "tax_amount", "exempt_amount"):
                portal_amount = float(portal.get(field) or 0)
                if portal_amount:
                    item[field] = portal_amount
        merged.append(item)

    added_invoices = 0
    skipped_existing_invoices = 0
    for folio, doc in portal_invoices_by_folio.items():
        key = _issued_doc_key(doc)
        folio_key = _folio_match_key(folio)
        if key in fns_keys or (
            folio_key and folio_key in fns_referenced_invoice_folios
        ):
            skipped_existing_invoices += 1
            continue
        item = dict(doc)
        item["source"] = "billing_portal"
        item["dte_version_code"] = "issued"
        raw = item.get("raw")
        if isinstance(raw, dict):
            raw = dict(raw)
            raw["billing_portal_only"] = True
            item["raw"] = raw
        else:
            item["raw"] = {"billing_portal_only": True, "portal": doc}
        merged.append(item)
        added_invoices += 1

    corroborated = 0
    for item in merged:
        if str(item.get("source") or "") != "extranet_web":
            continue
        key = _issued_doc_key(item)
        code = key[0]
        is_boleta = code in _BOLETA_TYPE_CODES or (
            not code and "boleta" in str(item.get("doc_type") or "").lower()
        )
        if not is_boleta or not key[1]:
            continue
        if key[1] not in portal_boleta_folios:
            continue
        corroborated += 1
        raw = item.get("raw")
        if isinstance(raw, dict):
            raw = dict(raw)
            raw["billing_portal_corroborated"] = True
            item["raw"] = raw
        else:
            item["raw"] = {"billing_portal_corroborated": True}

    parts = [
        f"FNS: {len(fns_docs)}",
        f"facturas nuevas facturacion.cl: {added_invoices}",
        f"facturas portal omitidas (ya en FNS): {skipped_existing_invoices}",
        f"RUT enriquecidos desde portal: {enriched_ruts}",
        f"boletas FNS corroboradas en portal: {corroborated}/{len(fns_boleta_folios)}",
    ]
    return merged, " · ".join(parts)

router = APIRouter(tags=["tax-documents"])

# Placeholder customer for issued docs without a real guest/client from FNS.
GENERIC_CUSTOMER_RUT = "66.666.666-6"
GENERIC_CUSTOMER_NAME = "Sin cliente"
_GENERIC_NAME_HINTS = {"", "consumos", "consumos consumos"}

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
    # Prefer explicit catalog code over free-text doc_type (FNS Documento can be a
    # folio list like "3398, 33..." which falsely substring-matches type 33 Factura).
    raw = item.get("dte_type_id")
    if raw is None or raw == "":
        raw = item.get("dte_type_code") or item.get("doc_type") or ""
    # Voucher stays Voucher; empty/unknown → Boleta.
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


def _received_auto_paid_through(today: date | None = None) -> date:
    """
    Received docs on or before this day are auto Imputada Pagada.

    Once November starts, October of that year (and earlier) is closed as paid.
    Before November, the cutoff stays last year's October 31.
    """
    day = today or date.today()
    year = day.year if day.month >= 11 else day.year - 1
    return date(year, 10, 31)


def _period_on_or_before_cutoff(period: str | None, cutoff: date) -> bool:
    """True when period text (MM-YYYY / YYYY-MM) is on or before cutoff month."""
    text = str(period or "").strip()
    if not text:
        return False
    month_first = re.match(r"^(\d{1,2})[-/](\d{4})$", text)
    if month_first:
        month = int(month_first.group(1))
        year = int(month_first.group(2))
    else:
        year_first = re.match(r"^(\d{4})[-/](\d{1,2})$", text)
        if not year_first:
            return False
        year = int(year_first.group(1))
        month = int(year_first.group(2))
    if month < 1 or month > 12:
        return False
    return date(year, month, 1) <= date(cutoff.year, cutoff.month, 1)


def _mark_received_through_cutoff_paid(db: Session) -> int:
    """Set Imputada Pagada on received docs dated through the October cutoff."""
    received_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_RECEIVED)
    paid_id = dte_catalog.paid_status_id(db)
    if received_id is None or paid_id is None:
        return 0
    cutoff = _received_auto_paid_through()
    updated = (
        db.query(TaxDocuments)
        .filter(
            TaxDocuments.dte_version_id == int(received_id),
            TaxDocuments.issued_at.isnot(None),
            TaxDocuments.issued_at <= cutoff,
            TaxDocuments.status_id != int(paid_id),
        )
        .update({TaxDocuments.status_id: int(paid_id)}, synchronize_session=False)
    )
    # Rows without issued_at: close by period (MM-YYYY) through cutoff month.
    orphan_updated = 0
    orphans = (
        db.query(TaxDocuments)
        .filter(
            TaxDocuments.dte_version_id == int(received_id),
            TaxDocuments.issued_at.is_(None),
            TaxDocuments.status_id != int(paid_id),
        )
        .all()
    )
    for row in orphans:
        if _period_on_or_before_cutoff(row.period, cutoff):
            row.status_id = int(paid_id)
            orphan_updated += 1
    total = int(updated or 0) + orphan_updated
    if total:
        db.commit()
    return total


def _normalize_rut_digits(value: object) -> str:
    return re.sub(r"[^0-9]", "", str(value or ""))


def _remove_utility_docs_from_issued(db: Session) -> int:
    """
    Enel / Aguas Andinas belong in Recibidas only.
    Move any issued leftovers (by source or catalog supplier RUT) to received.
    """
    issued_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_ISSUED)
    received_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_RECEIVED)
    if issued_id is None or received_id is None:
        return 0

    utility_ruts = {
        _normalize_rut_digits(row.get("supplier_rut"))
        for row in load_utility_bill_accounts()
        if _normalize_rut_digits(row.get("supplier_rut"))
    }
    rows = (
        db.query(TaxDocuments)
        .filter(TaxDocuments.dte_version_id == int(issued_id))
        .all()
    )
    moved = 0
    for row in rows:
        source = str(row.source or "").strip().lower()
        rut_digits = _normalize_rut_digits(row.identification_number)
        is_utility = source == "servipag" or (rut_digits and rut_digits in utility_ruts)
        if not is_utility:
            # Also catch manual/misfiled rows by company name hints in raw_json.
            raw = str(row.raw_json or "").lower()
            company_hint = "enel" in raw or "aguas andinas" in raw
            if not company_hint:
                continue
        row.dte_version_id = int(received_id)
        if source != "servipag":
            row.source = "servipag"
        moved += 1
    if moved:
        db.commit()
    return moved


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
    dte_catalog.TYPE_CODE_VOUCHER: "Voucher",
}


def _dte_type_name_es(*, code: str = "", name: str = "") -> str:
    code_key = str(code or "").strip()
    if code_key in _DTE_TYPE_LABELS_ES:
        return _DTE_TYPE_LABELS_ES[code_key]
    raw = str(name or "").strip()
    low = raw.lower()
    if low in {"invoice", "factura"}:
        return "Factura"
    if low in {"voucher", "vale"} or "voucher" in low:
        return "Voucher"
    if low in {"receipt", "boleta", "comprobante", ""}:
        return "Boleta"
    if "exempt" in low or "exenta" in low:
        return "Factura Exenta"
    if "credit" in low or "crédito" in low or "credito" in low:
        return "Nota de crédito"
    if "boleta" in low or "receipt" in low:
        return "Boleta"
    if "factura" in low or "invoice" in low:
        return "Factura"
    # Empty / unrecognized FNS labels → Boleta.
    return "Boleta"


def _concepts_have_amounts(concepts: object) -> bool:
    if not isinstance(concepts, dict):
        return False
    return any(abs(float(amount or 0)) > 0.5 for amount in concepts.values())


def _resolve_accounting_account_id(db: Session, item: dict) -> int | None:
    code = str(item.get("accounting_account_code") or "").strip()
    if code:
        row = (
            db.query(AccountingAccounts)
            .filter(AccountingAccounts.code == code)
            .first()
        )
        return int(row.id) if row is not None else None
    concepts = item.get("concepts")
    if not isinstance(concepts, dict):
        raw = item.get("raw")
        if isinstance(raw, dict):
            concepts = raw.get("concepts")
    accounts = load_income_accounts(db)
    if not accounts:
        return None
    # Everything from facturacion.cl → lodging (Hospedaje) from catalog.
    source = str(item.get("source") or "").strip().lower()
    if source == "billing_portal":
        account = lodging_income_account(accounts)
        return int(account.id) if account is not None else None
    # Cobros concept mix → best matching income account.
    if _concepts_have_amounts(concepts):
        account = pick_account_from_concepts(concepts, accounts)
        return int(account.id) if account is not None else None
    # Facturas/boletas without Cobros concepts default to lodging (Hospedaje) from catalog.
    account = lodging_income_account(accounts)
    return int(account.id) if account is not None else None


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
    # Older recibidas rows may only have TOTAL stored; fill Neto/IVA for the UI.
    if (
        abs(float(item.total_amount or 0)) > 0.5
        and abs(float(item.net_amount or 0)) <= 0.5
        and abs(float(item.tax_amount or 0)) <= 0.5
        and abs(float(item.exempt_amount or 0)) <= 0.5
    ):
        net, exempt, tax = _split_cl_iva(
            float(item.total_amount or 0),
            doc_type=item.dte_type_code or item.dte_type_name or "",
        )
        item.net_amount = net
        item.exempt_amount = exempt
        item.tax_amount = tax
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
    if issued_id and int(dte_version_id or 0) == int(issued_id):
        # Keep lodging/Hospedaje labels available for invoice defaults.
        ensure_concept_labels_from_coa(db)
    if received_id and int(dte_version_id or 0) == int(received_id):
        ensure_concept_labels_from_coa(db)
        ensure_received_document_account_mappings_from_catalog(db)
    received_account_map = (
        load_active_received_account_map(db) if received_id else {}
    )

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
        # Re-Actualizar: upgrade name:/empty placeholders when portal/FNS now has a real RUT.
        if (
            row is None
            and version_id == issued_id
            and folio
            and _is_real_rut(identification_number)
        ):
            candidates = (
                db.query(TaxDocuments)
                .filter(
                    TaxDocuments.dte_version_id == version_id,
                    TaxDocuments.dte_type_id == type_id,
                    TaxDocuments.folio == folio,
                )
                .all()
            )
            row = next(
                (
                    candidate
                    for candidate in candidates
                    if _is_placeholder_identification(candidate.identification_number)
                ),
                None,
            )
        payment_status_id = dte_catalog.status_id_from_payment_code(
            db, item.get("payment_status_code")
        )
        paid_id = dte_catalog.paid_status_id(db)
        raw_payload = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        item_source = str(item.get("source") or source or "").strip().lower()
        # FNS Informe Cobros rows are collected payments → always Imputada Pagada.
        is_fns_cobros = version_id == issued_id and (
            item_source in {"extranet_web", "extranet_collections"}
            or str(raw_payload.get("source_report") or "").strip().lower()
            == "collections"
            or str(raw_payload.get("fns_action") or "").strip().lower() == "cobros"
            or str(item.get("pms_class") or "").strip().lower() == "fns collections"
        )
        if is_fns_cobros and paid_id is not None:
            payment_status_id = int(paid_id)
        elif (
            str(item.get("payment_status_code") or "").strip().lower() == "paid"
            and item.get("has_concepts")
        ):
            payment_status_id = paid_id or payment_status_id
        received_auto_paid = (
            version_id == received_id
            and issued_at is not None
            and issued_at <= _received_auto_paid_through()
            and paid_id is not None
        )

        is_new = row is None
        if is_new:
            if version_id == received_id:
                if received_auto_paid:
                    initial_status_id = int(paid_id)
                elif payment_status_id:
                    initial_status_id = payment_status_id
                else:
                    initial_status_id = under_review_status_id
            else:
                initial_status_id = payment_status_id or default_status_id
            row = TaxDocuments(
                dte_version_id=version_id,
                dte_type_id=type_id,
                folio=folio,
                identification_number=identification_number,
                status_id=initial_status_id,
                period=period or "",
            )
            db.add(row)
        elif str(row.source or "").strip().lower() == "manual":
            # Keep operator-entered Track rows; do not overwrite from scrapes.
            continue

        row.dte_version_id = version_id
        row.dte_type_id = type_id
        # Issued + received: stamp period from document date (MM-YYYY).
        if period:
            row.period = period
        elif is_new or row.period is None:
            row.period = row.period or ""
        # Issued: update paid/pending from FNS. Received: keep user classification;
        # only default En revisión when status is empty. Exception: through October
        # cutoff → Imputada Pagada (closed months). Servipag utility rows may arrive
        # already marked pending_payment.
        if version_id == issued_id and payment_status_id:
            row.status_id = payment_status_id
        elif received_auto_paid:
            row.status_id = int(paid_id)
        elif (
            version_id == received_id
            and payment_status_id
            and str(item.get("source") or source or "").lower() == "servipag"
        ):
            row.status_id = payment_status_id
        elif not row.status_id:
            row.status_id = (
                under_review_status_id
                if version_id == received_id
                else (payment_status_id or default_status_id)
            )
        row.identification_number = identification_number
        row.issued_at = issued_at
        total_amount = float(item.get("total_amount") or 0)
        net_amount = float(item.get("net_amount") or 0)
        exempt_amount = float(item.get("exempt_amount") or 0)
        tax_amount = float(item.get("tax_amount") or 0)
        # Portal ledger often only has TOTAL; derive Neto/IVA (19%) when missing.
        doc_type_hint = (
            item.get("dte_type_code")
            or item.get("doc_type")
            or (item.get("raw") or {}).get("tipo")
            or (item.get("raw") or {}).get("DOCUMENTO")
            or ""
        )
        net_amount, exempt_amount, tax_amount = _split_cl_iva(
            total_amount,
            doc_type=doc_type_hint,
            net_amount=net_amount,
            exempt_amount=exempt_amount,
            tax_amount=tax_amount,
        )
        row.net_amount = net_amount
        row.exempt_amount = exempt_amount
        row.tax_amount = tax_amount
        row.total_amount = total_amount
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
            # Only fill when empty so manual classify is kept; invoices → Hospedaje by default.
            if not row.accounting_account_id:
                account_id = _resolve_accounting_account_id(db, item)
                if account_id:
                    row.accounting_account_id = account_id
        elif version_id == received_id:
            # Prefer explicit account from utility catalog, else RUT→cuenta mapping.
            explicit_account_id = item.get("accounting_account_id")
            if explicit_account_id:
                row.accounting_account_id = int(explicit_account_id)
            else:
                account_code = str(item.get("accounting_account_code") or "").strip()
                if account_code:
                    account = (
                        db.query(AccountingAccounts)
                        .filter(AccountingAccounts.code == account_code)
                        .first()
                    )
                    if account is not None:
                        row.accounting_account_id = int(account.id)
                if not row.accounting_account_id:
                    mapped_id = resolve_received_accounting_account_id(
                        db,
                        identification_number,
                        mapping=received_account_map,
                    )
                    if mapped_id:
                        row.accounting_account_id = mapped_id
        row.source = str(item.get("source") or source or "")[:40]
        try:
            row.raw_json = json.dumps(item.get("raw") or item, ensure_ascii=False, default=str)
        except TypeError:
            row.raw_json = None
        upserted += 1

    db.commit()
    return upserted, classified, suppliers_upserted, customers_upserted


def _backfill_unassigned_issued_to_lodging(
    db: Session,
    *,
    date_from: date,
    date_to: date,
) -> int:
    """
    On Emitidas → Actualizar only: assign lodging (Hospedaje) to issued docs
    still missing accounting_account_id in the refreshed date range.
    """
    ensure_concept_labels_from_coa(db)
    accounts = load_income_accounts(db)
    lodging = lodging_income_account(accounts)
    if lodging is None:
        return 0
    issued_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_ISSUED)
    if issued_id is None:
        return 0
    lodging_id = int(lodging.id)
    rows = (
        db.query(TaxDocuments)
        .filter(
            TaxDocuments.dte_version_id == int(issued_id),
            TaxDocuments.accounting_account_id.is_(None),
            TaxDocuments.issued_at.isnot(None),
            TaxDocuments.issued_at >= date_from,
            TaxDocuments.issued_at <= date_to,
        )
        .all()
    )
    if not rows:
        return 0
    for row in rows:
        row.accounting_account_id = lodging_id
    db.commit()
    return len(rows)


def _mark_issued_cobros_paid(
    db: Session,
    *,
    date_from: date,
    date_to: date,
) -> int:
    """Force Imputada Pagada on FNS Cobros issued rows in the refresh window."""
    issued_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_ISSUED)
    paid_id = dte_catalog.paid_status_id(db)
    if issued_id is None or paid_id is None:
        return 0
    rows = (
        db.query(TaxDocuments)
        .filter(
            TaxDocuments.dte_version_id == int(issued_id),
            TaxDocuments.issued_at.isnot(None),
            TaxDocuments.issued_at >= date_from,
            TaxDocuments.issued_at <= date_to,
            or_(
                func.lower(TaxDocuments.source) == "extranet_web",
                func.lower(TaxDocuments.source) == "extranet_collections",
                TaxDocuments.pms_class == "FNS Collections",
            ),
            or_(
                TaxDocuments.status_id.is_(None),
                TaxDocuments.status_id != int(paid_id),
            ),
        )
        .all()
    )
    if not rows:
        return 0
    for row in rows:
        row.status_id = int(paid_id)
    db.commit()
    return len(rows)


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
    # Never show Servipag utilities under Emitidas.
    issued_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_ISSUED)
    if issued_id is not None and int(dte_version_id) == int(issued_id):
        query = query.filter(
            or_(
                TaxDocuments.source.is_(None),
                func.lower(TaxDocuments.source) != "servipag",
            )
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
    # Seed monthly Piscina/Termos boletas even if startup seed was skipped.
    ensure_manual_received_boletas(db)
    # Keep closed months (through October) as Imputada Pagada without requiring Actualizar.
    _mark_received_through_cutoff_paid(db)
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


def _next_manual_folio(db: Session, *, version_id: int, type_id: int | None) -> str:
    """Generate a unique manual folio that will not collide with SII/FNS folios."""
    prefix = f"MAN-{date.today().strftime('%Y%m%d')}-"
    existing = (
        db.query(TaxDocuments.folio)
        .filter(
            TaxDocuments.dte_version_id == int(version_id),
            TaxDocuments.folio.like(f"{prefix}%"),
        )
        .all()
    )
    used = set()
    for (folio,) in existing:
        suffix = str(folio or "")[len(prefix) :]
        if suffix.isdigit():
            used.add(int(suffix))
    seq = 1
    while seq in used:
        seq += 1
    # Bound type_id only for uniqueness clarity in logs; folio itself is enough.
    _ = type_id
    return f"{prefix}{seq:03d}"[:40]


@router.post("/documents", response_model=TaxDocumentOut)
def create_manual_document(
    payload: TaxDocumentCreate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TaxDocumentOut:
    """Create a manual Track document (issued or received)."""
    try:
        version_id = dte_catalog.version_id_from_raw(db, payload.direction)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="direction must be issued or received",
        ) from exc

    type_id = dte_catalog.type_id_from_raw(db, payload.dte_type_code)
    if type_id is None:
        raise HTTPException(status_code=400, detail="dte_type_code inválido")

    type_code = ""
    type_obj = db.query(DteTypes).filter(DteTypes.id == int(type_id)).first()
    if type_obj is not None:
        type_code = str(type_obj.code or "")

    issued_at = payload.issued_at or date.today()
    period = str(payload.period or "").strip()[:40] or _period_from_issued_at(issued_at)
    folio = str(payload.folio or "").strip()[:40]
    if not folio:
        folio = _next_manual_folio(db, version_id=version_id, type_id=type_id)

    identification_number = str(payload.identification_number or "").strip()[:80]
    company_name = str(payload.company_name or "").strip()
    issued_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_ISSUED)
    received_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_RECEIVED)

    if int(version_id) == int(issued_id or 0):
        _, identification_number = _upsert_customer(
            db, identification_number, company_name
        )
        if identification_number == GENERIC_CUSTOMER_RUT:
            company_name = GENERIC_CUSTOMER_NAME
    else:
        if not identification_number and not company_name:
            raise HTTPException(
                status_code=400,
                detail="Indicá RUT o nombre del proveedor",
            )
        if identification_number:
            _upsert_supplier(db, identification_number, company_name)
        else:
            # Keep a stable key when only name is provided.
            identification_number = _customer_key("", company_name)

    total_amount = float(payload.total_amount or 0)
    if total_amount < 0:
        raise HTTPException(status_code=400, detail="total_amount inválido")
    net_amount, exempt_amount, tax_amount = _split_cl_iva(
        total_amount,
        doc_type=type_code or payload.dte_type_code,
    )

    status_id = payload.status_id
    if status_id is not None:
        status = db.query(DteStatuses).filter(DteStatuses.id == int(status_id)).first()
        if status is None:
            raise HTTPException(status_code=400, detail="status_id inválido")
    else:
        status_id = dte_catalog.default_new_document_status_id(db)

    accounting_account_id = payload.accounting_account_id
    if accounting_account_id is not None and int(accounting_account_id) != 0:
        account = (
            db.query(AccountingAccounts)
            .filter(AccountingAccounts.id == int(accounting_account_id))
            .first()
        )
        if account is None:
            raise HTTPException(status_code=400, detail="accounting_account_id inválido")
    else:
        accounting_account_id = None
        if int(version_id) == int(received_id or 0) and identification_number:
            accounting_account_id = resolve_received_accounting_account_id(
                db,
                identification_number=identification_number,
            )

    duplicate = (
        db.query(TaxDocuments)
        .filter(
            TaxDocuments.dte_version_id == int(version_id),
            TaxDocuments.dte_type_id == int(type_id),
            TaxDocuments.folio == folio,
            TaxDocuments.identification_number == identification_number,
        )
        .first()
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=400,
            detail="Ya existe un documento con ese folio, tipo y RUT",
        )

    row = TaxDocuments(
        dte_version_id=int(version_id),
        dte_type_id=int(type_id),
        folio=folio,
        identification_number=identification_number,
        status_id=int(status_id) if status_id is not None else None,
        accounting_account_id=int(accounting_account_id)
        if accounting_account_id is not None
        else None,
        period=period,
        issued_at=issued_at,
        net_amount=net_amount,
        exempt_amount=exempt_amount,
        tax_amount=tax_amount,
        total_amount=total_amount,
        branch="",
        pms_class="Manual",
        pms_ref="",
        source="manual",
        raw_json=json.dumps(
            {
                "manual": True,
                "company_name": company_name,
                "dte_type_code": type_code or payload.dte_type_code,
            },
            ensure_ascii=False,
        ),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="No se pudo guardar (conflicto de folio/RUT).",
        ) from exc

    row = (
        db.query(TaxDocuments)
        .options(
            joinedload(TaxDocuments.status),
            joinedload(TaxDocuments.dte_type),
            joinedload(TaxDocuments.dte_version),
            joinedload(TaxDocuments.accounting_account),
        )
        .filter(TaxDocuments.id == row.id)
        .first()
    )
    if int(version_id) == int(issued_id or 0):
        names = _customer_name_map(db, [row.identification_number])
    else:
        names = _supplier_name_map(db, [row.identification_number])
    return _serialize_doc(
        row,
        company_name=names.get(row.identification_number or "", "") or company_name,
    )


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

    if payload.comment is not None:
        row.comment = str(payload.comment or "").strip()[:255]

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


@router.post("/documents/mark-paid", response_model=TaxDocumentListResult)
def mark_documents_paid(
    payload: TaxDocumentBulkPaid,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TaxDocumentListResult:
    """Mark selected received documents as Imputada Pagada and optional payment comment."""
    ids = sorted({int(doc_id) for doc_id in (payload.document_ids or []) if int(doc_id) > 0})
    if not ids:
        raise HTTPException(status_code=400, detail="Seleccioná al menos un documento")
    paid_id = dte_catalog.paid_status_id(db)
    if paid_id is None:
        raise HTTPException(status_code=500, detail="Estado paid no configurado")
    comment = str(payload.comment or "").strip()[:255]
    rows = (
        db.query(TaxDocuments)
        .options(
            joinedload(TaxDocuments.status),
            joinedload(TaxDocuments.dte_type),
            joinedload(TaxDocuments.dte_version),
            joinedload(TaxDocuments.accounting_account),
        )
        .filter(TaxDocuments.id.in_(ids))
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Documentos no encontrados")
    for row in rows:
        row.status_id = int(paid_id)
        if comment:
            row.comment = comment
        db.add(row)
    db.commit()
    refreshed = (
        db.query(TaxDocuments)
        .options(
            joinedload(TaxDocuments.status),
            joinedload(TaxDocuments.dte_type),
            joinedload(TaxDocuments.dte_version),
            joinedload(TaxDocuments.accounting_account),
        )
        .filter(TaxDocuments.id.in_(ids))
        .all()
    )
    names = _supplier_name_map(db, [row.identification_number for row in refreshed])
    return TaxDocumentListResult(
        items=[
            _serialize_doc(
                row,
                company_name=names.get(row.identification_number or "", ""),
            )
            for row in refreshed
        ],
        total=len(refreshed),
        statuses=_list_statuses(db),
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


def _scrape_tax_documents_sync(
    *,
    version_id: int,
    received_id: int,
    date_from: date,
    date_to: date,
) -> tuple[list[dict[str, Any]], str, str, list[dict]]:
    """Blocking FNS/portal scrape — must run off the asyncio event loop."""
    warning = ""
    docs: list[dict[str, Any]] = []
    source = "extranet_web"
    pms_rows: list[dict] = []

    if version_id == received_id:
        # Recibidas = libro de compras (billing portal). Contabilidad extranet does not expose it.
        source = "web"
        with BillingPortalWebClient() as web:
            docs = web.fetch_received(date_from, date_to)
        # Also pull Enel / Aguas Andinas debts from Servipag into Recibidas.
        utility_docs, utility_warning = fetch_utility_received_documents()
        if utility_docs:
            docs.extend(utility_docs)
        if utility_warning:
            warning = (
                f"{warning} · Servipag: {utility_warning}"
                if warning
                else f"Servipag: {utility_warning}"
            )
    else:
        # Issued = FNS Informes → Cobros (Importe) + facturas from facturacion.cl.
        source = "extranet_collections"
        with TaxDocumentsFetchClient() as fetch_client:
            fns_docs = fetch_client.fetch_issued(date_from, date_to)

        portal_docs: list[dict] = []
        portal_error = ""
        try:
            with BillingPortalWebClient() as web:
                portal_docs = web.fetch_issued(date_from, date_to)
        except BillingPortalError as portal_exc:
            portal_error = str(portal_exc)
            try:
                portal_docs = BillingPortalClient().fetch_issued(
                    date_from, date_to, book_types=("V", "B")
                )
                portal_error = ""
            except BillingPortalError as soap_exc:
                portal_error = (
                    f"{portal_error}; SOAP: {soap_exc}"
                    if portal_error
                    else str(soap_exc)
                )

        docs, merge_warning = _merge_issued_with_billing_portal(fns_docs, portal_docs)
        warning = merge_warning
        if portal_error:
            warning = (
                f"{warning} · Portal: {portal_error}"
                if warning
                else f"Portal: {portal_error}"
            )

    return docs, source, warning, pms_rows


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

    try:
        # Keep the event loop free so login /auth/me and other views stay responsive
        # while FNS/facturacion scrapes run (can take several minutes).
        docs, source, warning, pms_rows = await asyncio.to_thread(
            _scrape_tax_documents_sync,
            version_id=version_id,
            received_id=received_id,
            date_from=date_from,
            date_to=date_to,
        )
    except BillingPortalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ExtranetWebError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    for doc in docs:
        doc["dte_version_id"] = version_id
        resolved_type = _resolve_type_id(db, doc)
        if resolved_type is not None:
            doc["dte_type_id"] = resolved_type

    accounts_assigned = 0
    try:
        issued_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_ISSUED)
        # Replace issued docs in the refreshed window so prior Cobros/portal
        # leftovers do not double-count beside income-collections reservation rows.
        if issued_id is not None and int(version_id) == int(issued_id):
            (
                db.query(TaxDocuments)
                .filter(
                    TaxDocuments.dte_version_id == int(issued_id),
                    TaxDocuments.issued_at.isnot(None),
                    TaxDocuments.issued_at >= date_from,
                    TaxDocuments.issued_at <= date_to,
                    or_(
                        TaxDocuments.source.is_(None),
                        func.lower(TaxDocuments.source) != "manual",
                    ),
                )
                .delete(synchronize_session=False)
            )
            db.commit()
            moved_utilities = _remove_utility_docs_from_issued(db)
            if moved_utilities:
                note = (
                    f"Se movieron {moved_utilities} doc(s) de Enel/Aguas "
                    "desde Emitidas a Recibidas."
                )
                warning = f"{warning} · {note}" if warning else note

        upserted, classified, suppliers_upserted, customers_upserted = _upsert_docs(
            db,
            docs,
            source=source,
            dte_version_id=version_id,
            pms_rows=pms_rows,
            match_fns=False,
        )
        # Explicit pass: any issued doc still without account in this range → Hospedaje.
        if issued_id is not None and int(version_id) == int(issued_id):
            accounts_assigned = _backfill_unassigned_issued_to_lodging(
                db, date_from=date_from, date_to=date_to
            )
            if accounts_assigned:
                note = (
                    f"Se asignó Hospedaje a {accounts_assigned} documento(s) "
                    "sin cuenta contable."
                )
                warning = f"{warning} · {note}" if warning else note
            cobros_paid = _mark_issued_cobros_paid(
                db, date_from=date_from, date_to=date_to
            )
            if cobros_paid:
                note = (
                    f"Se marcó Imputada Pagada en {cobros_paid} cobro(s) FNS."
                )
                warning = f"{warning} · {note}" if warning else note
        if received_id is not None and int(version_id) == int(received_id):
            moved_utilities = _remove_utility_docs_from_issued(db)
            if moved_utilities:
                note = (
                    f"Se movieron {moved_utilities} doc(s) de Enel/Aguas "
                    "desde Emitidas a Recibidas."
                )
                warning = f"{warning} · {note}" if warning else note
            paid_marked = _mark_received_through_cutoff_paid(db)
            if paid_marked:
                cutoff = _received_auto_paid_through()
                note = (
                    f"Se marcó Imputada Pagada en {paid_marked} recibida(s) "
                    f"hasta {cutoff.strftime('%d-%m-%Y')} (octubre inclusive)."
                )
                warning = f"{warning} · {note}" if warning else note
            seeded = ensure_manual_received_boletas(db)
            # Re-apply cutoff after seed so Oct manuals stay Imputada Pagada.
            paid_after_seed = _mark_received_through_cutoff_paid(db)
            if paid_after_seed and not paid_marked:
                cutoff = _received_auto_paid_through()
                note = (
                    f"Se marcó Imputada Pagada en {paid_after_seed} recibida(s) "
                    f"hasta {cutoff.strftime('%d-%m-%Y')} (octubre inclusive)."
                )
                warning = f"{warning} · {note}" if warning else note
            if seeded:
                note = (
                    f"Se cargaron/actualizaron {seeded} boleta(s) manuales "
                    "(Mantención / Electricidad / Agua)."
                )
                warning = f"{warning} · {note}" if warning else note
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
        accounts_assigned=accounts_assigned,
        warning=warning,
    )
