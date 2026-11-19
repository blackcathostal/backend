"""Resolve DTE type / version / status IDs from catalog tables (not hard-coded ints)."""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.models.tax_documents import DteStatuses, DteTypes, DteVersions

# Stable catalog codes stored in DB (seed). Runtime always looks up by these.
TYPE_CODE_INVOICE = "33"
TYPE_CODE_EXEMPT_INVOICE = "34"
TYPE_CODE_RECEIPT = "39"
TYPE_CODE_CREDIT_NOTE = "61"
# FNS internal label — always mapped to Boleta, never stored as its own catalog row.
TYPE_CODE_VOUCHER = "voucher"
VERSION_CODE_ISSUED = "issued"
VERSION_CODE_RECEIVED = "received"
STATUS_CODE_WAITING = "waiting"
STATUS_CODE_PENDING_PAYMENT = "pending_payment"
STATUS_CODE_PAID = "paid"

CANONICAL_TYPE_CODES = {
    TYPE_CODE_INVOICE,
    TYPE_CODE_EXEMPT_INVOICE,
    TYPE_CODE_RECEIPT,
    TYPE_CODE_CREDIT_NOTE,
}


def _norm(text: object) -> str:
    value = str(text or "").strip().lower()
    for src, dst in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n")):
        value = value.replace(src, dst)
    return value


def _slug_code(raw: object) -> str:
    text = _norm(raw)
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return (text or "unknown")[:40]


def type_id_by_code(db: Session, code: str) -> int | None:
    row = db.query(DteTypes).filter(DteTypes.code == str(code).strip()).first()
    return int(row.id) if row else None


def _alias_type_code(raw: object) -> str | None:
    """Map FNS/SII free-text labels to a canonical catalog code."""
    needle = _norm(raw)
    if not needle:
        return TYPE_CODE_RECEIPT
    if needle in {"voucher", "vale", "comprobante", "unknown", "otro", "other", "n/a", "-"}:
        return TYPE_CODE_RECEIPT
    if "voucher" in needle or "vale" in needle or "comprobante" in needle:
        return TYPE_CODE_RECEIPT
    if "exent" in needle:
        return TYPE_CODE_EXEMPT_INVOICE
    if "credito" in needle or "credit" in needle or needle in {"61", "nc"}:
        return TYPE_CODE_CREDIT_NOTE
    if "boleta" in needle or "receipt" in needle or needle in {"39"}:
        return TYPE_CODE_RECEIPT
    if "factura" in needle or "invoice" in needle or needle in {"33"}:
        return TYPE_CODE_INVOICE
    return None


def type_id_from_raw(db: Session, raw: object) -> int | None:
    """Map label/code/id input to canonical dte_types.id (33/34/39/61)."""
    text = str(raw or "").strip()
    needle = _norm(text)

    # Empty / voucher / unknown free-text → Boleta before any catalog lookup.
    alias = _alias_type_code(text)
    if not text or needle in {"voucher", "vale", "comprobante", "unknown", "otro", "other", "n/a", "-"}:
        return type_id_by_code(db, alias or TYPE_CODE_RECEIPT)
    if "voucher" in needle or "vale" in needle:
        return type_id_by_code(db, TYPE_CODE_RECEIPT)

    # Direct id / code match against canonical catalog only
    if text.isdigit():
        by_id = db.query(DteTypes).filter(DteTypes.id == int(text)).first()
        if by_id is not None and str(by_id.code) in CANONICAL_TYPE_CODES:
            return int(by_id.id)
        by_code = type_id_by_code(db, text)
        if by_code is not None:
            return by_code

    by_code = type_id_by_code(db, text)
    if by_code is not None:
        row = db.query(DteTypes).filter(DteTypes.id == by_code).first()
        if row is not None and str(row.code) in CANONICAL_TYPE_CODES:
            return by_code
        # Leftover non-canonical rows (e.g. voucher) → Boleta
        return type_id_by_code(db, TYPE_CODE_RECEIPT)

    rows = db.query(DteTypes).filter(DteTypes.code.in_(sorted(CANONICAL_TYPE_CODES))).all()
    for row in rows:
        if _norm(row.code) == needle or _norm(row.name) == needle:
            return int(row.id)
    for row in rows:
        code_n = _norm(row.code)
        name_n = _norm(row.name)
        if code_n and (code_n in needle or needle in code_n):
            return int(row.id)
        if name_n and (name_n in needle or needle in name_n):
            return int(row.id)

    if alias:
        return type_id_by_code(db, alias)
    return None


def ensure_type_id(db: Session, raw: object) -> int | None:
    """Resolve type from canonical catalog only. Never create Voucher/unknown rows."""
    existing = type_id_from_raw(db, raw)
    if existing is not None:
        return existing
    alias = _alias_type_code(raw) or TYPE_CODE_RECEIPT
    return type_id_by_code(db, alias)


def paid_status_id(db: Session) -> int | None:
    return status_id_by_code(db, STATUS_CODE_PAID)


def status_id_from_payment_code(db: Session, code: object) -> int | None:
    key = _norm(code)
    if not key:
        return None
    # Prefer exact catalog code match first.
    by_code = status_id_by_code(db, key)
    if by_code is not None:
        return by_code
    rows = db.query(DteStatuses).all()
    for row in rows:
        if _norm(row.code) == key or _norm(row.name) == key:
            return int(row.id)
    for row in rows:
        name_n = _norm(row.name)
        code_n = _norm(row.code)
        if name_n and (name_n in key or key in name_n):
            return int(row.id)
        if code_n and (code_n in key or key in code_n):
            return int(row.id)
    return None


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
    """New documents start as under review until the user classifies payment."""
    return waiting_status_id(db) or pending_payment_status_id(db)


def under_review_status_id(db: Session) -> int | None:
    """Alias: waiting catalog row is shown as En revisión."""
    return waiting_status_id(db)


def all_type_ids(db: Session) -> set[int]:
    return {int(row.id) for row in db.query(DteTypes.id).all()}


def all_version_ids(db: Session) -> set[int]:
    return {int(row.id) for row in db.query(DteVersions.id).all()}
