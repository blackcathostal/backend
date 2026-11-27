"""Seed monthly received boletas from app/data/manual_received_boletas.json."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from sqlalchemy.exc import IntegrityError

from app.models.accounting_accounts import AccountingAccounts
from app.models.suppliers import Suppliers
from app.models.tax_documents import TaxDocuments
from app.services import dte_catalog
from app.services.received_document_account_map import normalize_identification_number
from app.services.tax_documents_fetch import _split_cl_iva

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[2]
CATALOG_PATH = BASE_DIR / "app" / "data" / "manual_received_boletas.json"

# November stays En revisión; every other month is Imputada Pagada.
OPEN_REVIEW_MONTH = 11

# Invented Mantención Termos RUT + retired folio prefixes (Termos/Enel/Aguas seeds).
# Real Enel/Aguas FNS docs keep their RUTs; only these manual seed folios are purged.
_RETIRED_SEED_RUTS = frozenset(
    {normalize_identification_number("76.881.234-9")}
)
_RETIRED_FOLIO_PREFIXES = ("888", "7777", "6666")


def is_retired_manual_seed(
    *,
    identification_number: object = None,
    folio: object = None,
    source: object = None,
    raw_json: object = None,
    pms_class: object = None,
) -> bool:
    """True for invented Termos / retired Enel-Agua manual seed boletas."""
    src = str(source or "").strip().lower()
    raw = str(raw_json or "")
    pms = str(pms_class or "")
    is_seed = src == "manual" or "manual_received_boletas" in raw or pms == "Manual"
    if not is_seed:
        return False
    rut = normalize_identification_number(identification_number)
    folio_text = str(folio or "").strip()
    if rut in _RETIRED_SEED_RUTS:
        return True
    return any(folio_text.startswith(prefix) for prefix in _RETIRED_FOLIO_PREFIXES)


def _load_catalog() -> list[dict]:
    if not CATALOG_PATH.exists():
        return []
    try:
        rows = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("manual received boletas catalog unreadable: %s", exc)
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("active", True)]


def _months_for_item(item: dict) -> list[int]:
    """Months to seed (1–12). Optional catalog keys: months / exclude_months."""
    raw_months = item.get("months")
    if isinstance(raw_months, list) and raw_months:
        months: list[int] = []
        for value in raw_months:
            try:
                month = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= month <= 12:
                months.append(month)
        if months:
            return sorted(set(months))

    excluded: set[int] = set()
    for value in item.get("exclude_months") or []:
        try:
            month = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= month <= 12:
            excluded.add(month)
    return [month for month in range(1, 13) if month not in excluded]


def ensure_manual_received_boletas(db: Session) -> int:
    """
    Upsert one boleta per month for each catalog supplier.
    Folio = prefix + MM (e.g. 999901, 88811). November → waiting; else → paid.
    """
    catalog = _load_catalog()
    if not catalog:
        return 0

    # CoA must exist first (Luz/Agua 5310/5311, Mantención, …).
    try:
        from app.services.accounting_concept_map import ensure_concept_labels_from_coa

        ensure_concept_labels_from_coa(db)
    except Exception:  # noqa: BLE001
        logger.warning("manual boletas: CoA sync skipped", exc_info=True)

    received_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_RECEIVED)
    receipt_type_id = dte_catalog.type_id_by_code(db, dte_catalog.TYPE_CODE_RECEIPT)
    waiting_id = dte_catalog.waiting_status_id(db)
    paid_id = dte_catalog.paid_status_id(db)
    if received_id is None or receipt_type_id is None or waiting_id is None or paid_id is None:
        return 0

    accounts_by_code = {
        str(row.code or "").strip(): row
        for row in db.query(AccountingAccounts).filter(AccountingAccounts.is_active.is_(True)).all()
        if str(row.code or "").strip()
    }

    # Wipe retired seeds first (Termos / old Enel-Agua). Do not wipe active catalog
    # rows here — empty keep_keys used to delete everything and race to $0 in EERR.
    changed = _purge_retired_manual_boletas(
        db,
        received_id=int(received_id),
        keep_keys=set(),
        retired_only=True,
    )
    if changed:
        db.flush()

    keep_keys: set[tuple[str, str]] = set()
    for item in catalog:
        rut = normalize_identification_number(item.get("identification_number"))
        company_name = str(item.get("company_name") or "").strip()[:180]
        prefix = str(item.get("folio_prefix") or "").strip()
        account_code = str(item.get("accounting_account_code") or "").strip()
        try:
            year = int(item.get("year") or date.today().year)
            total_amount = float(item.get("total_amount") or 0)
        except (TypeError, ValueError):
            continue
        if not rut or not prefix or total_amount <= 0:
            continue
        if rut in _RETIRED_SEED_RUTS or any(
            prefix.startswith(retired) for retired in _RETIRED_FOLIO_PREFIXES
        ):
            continue

        type_code = str(item.get("dte_type_code") or "39").strip() or "39"
        type_id = dte_catalog.type_id_by_code(db, type_code) or receipt_type_id
        account = accounts_by_code.get(account_code)
        account_id = int(account.id) if account is not None else None
        net_amount, exempt_amount, tax_amount = _split_cl_iva(
            total_amount,
            doc_type=type_code,
        )

        if company_name:
            _ensure_supplier(db, rut, company_name)

        for month in _months_for_item(item):
            folio = f"{prefix}{month:02d}"[:40]
            keep_keys.add((rut, folio))
            issued_at = date(year, month, 1)
            period = f"{month:02d}-{year}"
            status_id = int(waiting_id) if month == OPEN_REVIEW_MONTH else int(paid_id)

            row = (
                db.query(TaxDocuments)
                .filter(
                    TaxDocuments.dte_version_id == int(received_id),
                    TaxDocuments.dte_type_id == int(type_id),
                    TaxDocuments.folio == folio,
                    TaxDocuments.identification_number == rut,
                )
                .first()
            )
            if row is None:
                row = TaxDocuments(
                    dte_version_id=int(received_id),
                    dte_type_id=int(type_id),
                    folio=folio,
                    identification_number=rut,
                    status_id=status_id,
                    accounting_account_id=account_id,
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
                    comment="",
                    raw_json=json.dumps(
                        {
                            "manual": True,
                            "seed": "manual_received_boletas",
                            "company_name": company_name,
                            "dte_type_code": type_code,
                        },
                        ensure_ascii=False,
                    ),
                )
                db.add(row)
                changed += 1
                continue

            row_changed = False
            if row.status_id != status_id:
                row.status_id = status_id
                row_changed = True
            if account_id and row.accounting_account_id != account_id:
                row.accounting_account_id = account_id
                row_changed = True
            if abs(float(row.total_amount or 0) - total_amount) > 0.5:
                row.total_amount = total_amount
                row.net_amount = net_amount
                row.exempt_amount = exempt_amount
                row.tax_amount = tax_amount
                row_changed = True
            if row.period != period:
                row.period = period
                row_changed = True
            if row.issued_at != issued_at:
                row.issued_at = issued_at
                row_changed = True
            if str(row.source or "") != "manual":
                row.source = "manual"
                row_changed = True
            if row_changed:
                changed += 1

    # Drop retired manual seeds (Termos / Enel / Agua, etc.) no longer in catalog.
    changed += _purge_retired_manual_boletas(
        db, received_id=int(received_id), keep_keys=keep_keys
    )

    if changed:
        db.commit()
    return changed


def _purge_retired_manual_boletas(
    db: Session,
    *,
    received_id: int,
    keep_keys: set[tuple[str, str]],
    retired_only: bool = False,
) -> int:
    """Delete seeded manual boletas that are retired and/or no longer in the catalog."""
    rows = (
        db.query(TaxDocuments)
        .filter(
            TaxDocuments.dte_version_id == int(received_id),
            TaxDocuments.source == "manual",
        )
        .all()
    )
    deleted = 0
    for row in rows:
        raw = str(row.raw_json or "")
        is_seed = "manual_received_boletas" in raw or str(row.pms_class or "") == "Manual"
        if not is_seed:
            continue
        rut = normalize_identification_number(row.identification_number)
        folio = str(row.folio or "").strip()
        key = (rut, folio)
        retired = rut in _RETIRED_SEED_RUTS or any(
            folio.startswith(prefix) for prefix in _RETIRED_FOLIO_PREFIXES
        )
        if retired_only:
            if not retired:
                continue
        elif key in keep_keys and not retired:
            continue
        db.delete(row)
        deleted += 1
    return deleted


def _ensure_supplier(db: Session, identification_number: str, company_name: str) -> None:
    row = (
        db.query(Suppliers)
        .filter(Suppliers.identification_number == identification_number)
        .first()
    )
    if row is None:
        try:
            with db.begin_nested():
                db.add(
                    Suppliers(
                        identification_number=identification_number,
                        company_name=company_name,
                    )
                )
                db.flush()
        except IntegrityError:
            row = (
                db.query(Suppliers)
                .filter(Suppliers.identification_number == identification_number)
                .first()
            )
            if row is not None and company_name and row.company_name != company_name:
                row.company_name = company_name
        return
    if company_name and row.company_name != company_name:
        row.company_name = company_name
