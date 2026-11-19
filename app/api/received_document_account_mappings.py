from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.accounting_accounts import AccountingAccounts
from app.models.received_document_account_mappings import ReceivedDocumentAccountMappings
from app.models.users import Users
from app.schemas.received_document_account_mappings import (
    ReceivedDocumentAccountMappingsCreate,
    ReceivedDocumentAccountMappingsOut,
    ReceivedDocumentAccountMappingsUpdate,
)
from app.services.received_document_account_map import (
    get_mapping_with_account,
    normalize_identification_number,
    serialize_mapping_row,
)

router = APIRouter(
    prefix="/received-document-account-mappings",
    tags=["received-document-account-mappings"],
)


def _require_active_account(db: Session, account_id: int) -> AccountingAccounts:
    account = (
        db.query(AccountingAccounts)
        .filter(AccountingAccounts.id == account_id)
        .first()
    )
    if not account:
        raise HTTPException(status_code=400, detail="Cuenta contable no encontrada")
    if not account.is_active:
        raise HTTPException(status_code=400, detail="La cuenta contable está inactiva")
    return account


def _ensure_unique_rut(
    db: Session, identification_number: str, *, exclude_id: int | None = None
) -> None:
    query = db.query(ReceivedDocumentAccountMappings).filter(
        ReceivedDocumentAccountMappings.identification_number == identification_number
    )
    if exclude_id is not None:
        query = query.filter(ReceivedDocumentAccountMappings.id != exclude_id)
    if query.first():
        raise HTTPException(
            status_code=400,
            detail="Ya existe un mapeo para ese RUT / identificador",
        )


@router.get("/", response_model=list[ReceivedDocumentAccountMappingsOut])
def list_received_document_account_mappings(
    active_only: bool = False,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[dict]:
    query = db.query(ReceivedDocumentAccountMappings).options(
        joinedload(ReceivedDocumentAccountMappings.accounting_account)
    )
    if active_only:
        query = query.filter(ReceivedDocumentAccountMappings.is_active.is_(True))
    rows = query.order_by(
        ReceivedDocumentAccountMappings.company_name.asc(),
        ReceivedDocumentAccountMappings.identification_number.asc(),
        ReceivedDocumentAccountMappings.id.asc(),
    ).all()
    return [serialize_mapping_row(row) for row in rows]


@router.get("/{mapping_id}", response_model=ReceivedDocumentAccountMappingsOut)
def get_received_document_account_mapping(
    mapping_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> dict:
    row = get_mapping_with_account(db, mapping_id)
    if not row:
        raise HTTPException(status_code=404, detail="Mapeo no encontrado")
    return serialize_mapping_row(row)


@router.post(
    "/",
    response_model=ReceivedDocumentAccountMappingsOut,
    status_code=status.HTTP_201_CREATED,
)
def create_received_document_account_mapping(
    payload: ReceivedDocumentAccountMappingsCreate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> dict:
    identification_number = normalize_identification_number(payload.identification_number)
    if not identification_number:
        raise HTTPException(status_code=400, detail="El RUT / identificador es obligatorio")
    _require_active_account(db, int(payload.accounting_account_id))
    _ensure_unique_rut(db, identification_number)

    row = ReceivedDocumentAccountMappings(
        identification_number=identification_number,
        company_name=str(payload.company_name or "").strip()[:180],
        accounting_account_id=int(payload.accounting_account_id),
        notes=(str(payload.notes).strip() if payload.notes else None) or None,
        is_active=bool(payload.is_active),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Ya existe un mapeo para ese RUT / identificador",
        ) from exc
    db.refresh(row)
    row = get_mapping_with_account(db, row.id)
    return serialize_mapping_row(row)


@router.put("/{mapping_id}", response_model=ReceivedDocumentAccountMappingsOut)
def update_received_document_account_mapping(
    mapping_id: int,
    payload: ReceivedDocumentAccountMappingsUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> dict:
    row = (
        db.query(ReceivedDocumentAccountMappings)
        .filter(ReceivedDocumentAccountMappings.id == mapping_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Mapeo no encontrado")

    data = payload.model_dump(exclude_unset=True)
    if "identification_number" in data:
        identification_number = normalize_identification_number(data.get("identification_number"))
        if not identification_number:
            raise HTTPException(status_code=400, detail="El RUT / identificador es obligatorio")
        _ensure_unique_rut(db, identification_number, exclude_id=row.id)
        row.identification_number = identification_number
    if "company_name" in data:
        row.company_name = str(data.get("company_name") or "").strip()[:180]
    if "accounting_account_id" in data and data.get("accounting_account_id") is not None:
        account_id = int(data["accounting_account_id"])
        _require_active_account(db, account_id)
        row.accounting_account_id = account_id
    if "notes" in data:
        notes = data.get("notes")
        row.notes = (str(notes).strip() if notes is not None else None) or None
    if "is_active" in data and data.get("is_active") is not None:
        row.is_active = bool(data["is_active"])

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Ya existe un mapeo para ese RUT / identificador",
        ) from exc
    row = get_mapping_with_account(db, mapping_id)
    return serialize_mapping_row(row)


@router.delete("/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_received_document_account_mapping(
    mapping_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> None:
    row = (
        db.query(ReceivedDocumentAccountMappings)
        .filter(ReceivedDocumentAccountMappings.id == mapping_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Mapeo no encontrado")
    db.delete(row)
    db.commit()
