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
TYPE_CODE_VOUCHER = "voucher"
VERSION_CODE_ISSUED = "issued"
VERSION_CODE_RECEIVED = "received"
STATUS_CODE_WAITING = "waiting"
STATUS_CODE_PENDING_PAYMENT = "pending_payment"
STATUS_CODE_PAID = "paid"


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


def type_id_from_raw(db: Session, raw: object) -> int | None:
    """Map label/code/id input to dte_types.id using only catalog rows in DB."""
    text = str(raw or "").strip()
    if not text:
        return None

    # Direct id / code match against catalog
    if text.isdigit():
        by_id = db.query(DteTypes).filter(DteTypes.id == int(text)).first()
        if by_id is not None:
            return int(by_id.id)
        by_code = type_id_by_code(db, text)
        if by_code is not None:
            return by_code

    by_code = type_id_by_code(db, text)
    if by_code is not None:
        return by_code

    needle = _norm(text)
    rows = db.query(DteTypes).all()
    # Exact normalized name/code
    for row in rows:
        if _norm(row.code) == needle or _norm(row.name) == needle:
            return int(row.id)
    # Contained match (FNS "Boleta electrónica" vs catalog "Boleta")
    for row in rows:
        code_n = _norm(row.code)
        name_n = _norm(row.name)
        if code_n and (code_n in needle or needle in code_n):
            return int(row.id)
        if name_n and (name_n in needle or needle in name_n):
            return int(row.id)
    return None


def ensure_type_id(db: Session, raw: object) -> int | None:
    """Resolve type from catalog; if missing, create from the raw FNS/SII label."""
    existing = type_id_from_raw(db, raw)
    if existing is not None:
        return existing
    label = str(raw or "").strip()
    if not label:
        return None
    code = _slug_code(label)
    row = db.query(DteTypes).filter(DteTypes.code == code).first()
    if row is not None:
        return int(row.id)
    row = DteTypes(code=code, name=label[:80])
    db.add(row)
    db.flush()
    return int(row.id)


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
