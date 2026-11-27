"""Resolve accounting accounts for received tax documents from DB catalog."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from sqlalchemy.orm import Session, joinedload

from app.models.accounting_accounts import AccountingAccounts
from app.models.received_document_account_mappings import ReceivedDocumentAccountMappings
from app.models.tax_documents import TaxDocuments
from app.services import dte_catalog

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[2]
CATALOG_PATH = BASE_DIR / "app" / "data" / "received_document_account_mappings.json"


def normalize_identification_number(value: str | None) -> str:
    """Normalize Chilean RUT / supplier id for stable matching."""
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    # Keep digits, K, and a single dash before the check digit when present.
    cleaned = re.sub(r"[^0-9K\-]", "", raw)
    cleaned = cleaned.replace("--", "-")
    return cleaned[:80]


def _load_catalog_rows() -> list[dict]:
    if not CATALOG_PATH.exists():
        return []
    try:
        rows = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("received account mapping catalog unreadable: %s", exc)
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def ensure_received_document_account_mappings_from_catalog(db: Session) -> int:
    """
    Upsert RUT→account mappings from app/data/received_document_account_mappings.json.
    Also assigns the mapped account to received docs that still lack one.
    """
    catalog = _load_catalog_rows()
    if not catalog:
        return 0

    accounts_by_code = {
        str(row.code or "").strip(): row
        for row in db.query(AccountingAccounts).all()
        if str(row.code or "").strip()
    }
    existing = {
        normalize_identification_number(row.identification_number): row
        for row in db.query(ReceivedDocumentAccountMappings).all()
        if normalize_identification_number(row.identification_number)
    }

    changed = 0
    for item in catalog:
        key = normalize_identification_number(item.get("identification_number"))
        account_code = str(item.get("accounting_account_code") or "").strip()
        if not key or not account_code:
            continue
        account = accounts_by_code.get(account_code)
        if account is None or not account.is_active:
            logger.warning(
                "received mapping skip %s: account code %s missing/inactive",
                key,
                account_code,
            )
            continue

        company_name = str(item.get("company_name") or "").strip()[:180]
        notes = str(item.get("notes") or "").strip() or None
        is_active = bool(item.get("active", True))
        account_id = int(account.id)
        row = existing.get(key)
        if row is None:
            row = ReceivedDocumentAccountMappings(
                identification_number=key,
                company_name=company_name,
                accounting_account_id=account_id,
                notes=notes,
                is_active=is_active,
            )
            db.add(row)
            existing[key] = row
            changed += 1
            continue

        row_changed = False
        if row.identification_number != key:
            row.identification_number = key
            row_changed = True
        if company_name and (row.company_name or "") != company_name:
            row.company_name = company_name
            row_changed = True
        if int(row.accounting_account_id or 0) != account_id:
            row.accounting_account_id = account_id
            row_changed = True
        if (row.notes or None) != notes:
            row.notes = notes
            row_changed = True
        if bool(row.is_active) != is_active:
            row.is_active = is_active
            row_changed = True
        if row_changed:
            changed += 1

    if changed:
        db.commit()

    changed += _backfill_received_accounts_from_map(db)
    return changed


def _backfill_received_accounts_from_map(db: Session) -> int:
    """Assign mapped accounts to received docs that still have no accounting_account_id."""
    mapping = load_active_received_account_map(db)
    if not mapping:
        return 0
    received_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_RECEIVED)
    if received_id is None:
        return 0

    updated = 0
    rows = (
        db.query(TaxDocuments)
        .filter(
            TaxDocuments.dte_version_id == int(received_id),
            TaxDocuments.accounting_account_id.is_(None),
        )
        .all()
    )
    for row in rows:
        account_id = mapping.get(normalize_identification_number(row.identification_number))
        if not account_id:
            continue
        row.accounting_account_id = int(account_id)
        updated += 1
    if updated:
        db.commit()
    return updated


def load_active_received_account_map(db: Session) -> dict[str, int]:
    """Return {normalized_identification_number: accounting_account_id} for active rows."""
    rows = (
        db.query(ReceivedDocumentAccountMappings)
        .filter(ReceivedDocumentAccountMappings.is_active.is_(True))
        .all()
    )
    mapping: dict[str, int] = {}
    for row in rows:
        key = normalize_identification_number(row.identification_number)
        if not key:
            continue
        account_id = int(row.accounting_account_id or 0)
        if account_id > 0:
            mapping[key] = account_id
    return mapping


def resolve_received_accounting_account_id(
    db: Session,
    identification_number: str | None,
    *,
    mapping: dict[str, int] | None = None,
) -> int | None:
    key = normalize_identification_number(identification_number)
    if not key:
        return None
    table = mapping if mapping is not None else load_active_received_account_map(db)
    account_id = table.get(key)
    if not account_id:
        return None
    active = (
        db.query(AccountingAccounts.id)
        .filter(
            AccountingAccounts.id == account_id,
            AccountingAccounts.is_active.is_(True),
        )
        .first()
    )
    return int(account_id) if active else None


def serialize_mapping_row(row: ReceivedDocumentAccountMappings) -> dict:
    account = row.accounting_account
    return {
        "id": row.id,
        "identification_number": row.identification_number,
        "company_name": row.company_name or "",
        "accounting_account_id": row.accounting_account_id,
        "account_code": (account.code if account else "") or "",
        "account_name": (account.name if account else "") or "",
        "notes": row.notes,
        "is_active": bool(row.is_active),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def get_mapping_with_account(
    db: Session, mapping_id: int
) -> ReceivedDocumentAccountMappings | None:
    return (
        db.query(ReceivedDocumentAccountMappings)
        .options(joinedload(ReceivedDocumentAccountMappings.accounting_account))
        .filter(ReceivedDocumentAccountMappings.id == mapping_id)
        .first()
    )
