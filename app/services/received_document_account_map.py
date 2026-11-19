"""Resolve accounting accounts for received tax documents from DB catalog."""

from __future__ import annotations

import re

from sqlalchemy.orm import Session, joinedload

from app.models.accounting_accounts import AccountingAccounts
from app.models.received_document_account_mappings import ReceivedDocumentAccountMappings


def normalize_identification_number(value: str | None) -> str:
    """Normalize Chilean RUT / supplier id for stable matching."""
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    # Keep digits, K, and a single dash before the check digit when present.
    cleaned = re.sub(r"[^0-9K\-]", "", raw)
    cleaned = cleaned.replace("--", "-")
    return cleaned[:80]


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
