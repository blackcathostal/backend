"""Resolve DTE type / version / status IDs from catalog tables (not hard-coded ints)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.tax_documents import DteStatuses, DteTypes, DteVersions

# Stable catalog codes stored in DB (seed). Runtime always looks up by these.
TYPE_CODE_INVOICE = "33"
TYPE_CODE_EXEMPT_INVOICE = "34"
TYPE_CODE_RECEIPT = "39"
TYPE_CODE_CREDIT_NOTE = "61"
VERSION_CODE_ISSUED = "issued"
VERSION_CODE_RECEIVED = "received"
STATUS_CODE_WAITING = "waiting"
STATUS_CODE_PENDING_PAYMENT = "pending_payment"
STATUS_CODE_PAID = "paid"


def type_id_by_code(db: Session, code: str) -> int | None:
    row = db.query(DteTypes).filter(DteTypes.code == str(code).strip()).first()
    return int(row.id) if row else None


def type_id_from_raw(db: Session, raw: object) -> int | None:
    """Map SII/label/id input to dte_types.id via table codes."""
    text = str(raw or "").strip().lower()
    if not text:
        return None

    # Direct id match against catalog
    if text.isdigit():
        by_id = db.query(DteTypes).filter(DteTypes.id == int(text)).first()
        if by_id is not None:
            return int(by_id.id)
        by_code = type_id_by_code(db, text)
        if by_code is not None:
            return by_code

    invoice_id = type_id_by_code(db, TYPE_CODE_INVOICE)
    exempt_id = type_id_by_code(db, TYPE_CODE_EXEMPT_INVOICE)
    receipt_id = type_id_by_code(db, TYPE_CODE_RECEIPT)
    credit_note_id = type_id_by_code(db, TYPE_CODE_CREDIT_NOTE)

    digits = "".join(ch for ch in text if ch.isdigit())
    if digits.startswith(TYPE_CODE_CREDIT_NOTE) or TYPE_CODE_CREDIT_NOTE in digits:
        return credit_note_id or invoice_id
    if digits.startswith(TYPE_CODE_EXEMPT_INVOICE) or TYPE_CODE_EXEMPT_INVOICE in digits:
        return exempt_id or invoice_id
    if digits.startswith(TYPE_CODE_RECEIPT) or TYPE_CODE_RECEIPT in digits:
        return receipt_id
    if digits.startswith(TYPE_CODE_INVOICE) or TYPE_CODE_INVOICE in digits:
        return invoice_id
    if "nota de credito" in text or "nota de crédito" in text or "credit note" in text:
        return credit_note_id or invoice_id
    if "exenta" in text or "exempt" in text:
        return exempt_id or invoice_id
    if "boleta" in text or "receipt" in text:
        return receipt_id
    if "factura" in text or "invoice" in text:
        return invoice_id
    return invoice_id


def version_id_by_code(db: Session, code: str) -> int | None:
    row = db.query(DteVersions).filter(DteVersions.code == str(code).strip().lower()).first()
    return int(row.id) if row else None


def version_id_from_raw(db: Session, raw: object, *, default_code: str = VERSION_CODE_ISSUED) -> int:
    key = str(raw if raw is not None else default_code).strip().lower()
    aliases = {
        VERSION_CODE_ISSUED: VERSION_CODE_ISSUED,
        "emitida": VERSION_CODE_ISSUED,
        "emitido": VERSION_CODE_ISSUED,
        "1": VERSION_CODE_ISSUED,
        VERSION_CODE_RECEIVED: VERSION_CODE_RECEIVED,
        "recibida": VERSION_CODE_RECEIVED,
        "recibido": VERSION_CODE_RECEIVED,
        "2": VERSION_CODE_RECEIVED,
    }
    code = aliases.get(key, default_code)
    version_id = version_id_by_code(db, code)
    if version_id is None:
        # Fallback: try numeric id already stored in catalog
        if key.isdigit():
            row = db.query(DteVersions).filter(DteVersions.id == int(key)).first()
            if row is not None:
                return int(row.id)
        raise ValueError(f"Unknown dte_versions code: {raw!r}")
    return version_id


def status_id_by_code(db: Session, code: str) -> int | None:
    row = db.query(DteStatuses).filter(DteStatuses.code == str(code).strip().lower()).first()
    return int(row.id) if row else None


def waiting_status_id(db: Session) -> int | None:
    return status_id_by_code(db, STATUS_CODE_WAITING)


def pending_payment_status_id(db: Session) -> int | None:
    return status_id_by_code(db, STATUS_CODE_PENDING_PAYMENT)


def default_new_document_status_id(db: Session) -> int | None:
    """New documents start as waiting until the user classifies payment."""
    return waiting_status_id(db) or pending_payment_status_id(db)


def all_type_ids(db: Session) -> set[int]:
    return {int(row.id) for row in db.query(DteTypes.id).all()}


def all_version_ids(db: Session) -> set[int]:
    return {int(row.id) for row in db.query(DteVersions.id).all()}
