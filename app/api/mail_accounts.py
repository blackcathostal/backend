from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.mail_accounts import MailAccounts
from app.models.users import Users
from app.schemas.mail_accounts import (
    MailAccountsCreate,
    MailAccountsOut,
    MailAccountsUpdate,
    SignatureImageOut,
)

router = APIRouter(prefix="/mail-accounts", tags=["mail-accounts"])

ALLOWED_SIGNATURE_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}
MAX_SIGNATURE_IMAGE_BYTES = 5 * 1024 * 1024


def _clear_other_defaults(db: Session, keep_id: int | None = None) -> None:
    query = db.query(MailAccounts).filter(MailAccounts.is_default.is_(True))
    if keep_id is not None:
        query = query.filter(MailAccounts.id != keep_id)
    for account in query.all():
        account.is_default = False


@router.post(
    "/signature-image",
    response_model=SignatureImageOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_signature_image(
    file: UploadFile = File(...),
    _: Users = Depends(get_current_user),
) -> SignatureImageOut:
    content_type = (file.content_type or "").lower().strip()
    if content_type not in ALLOWED_SIGNATURE_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Formato no permitido. Usa JPG, PNG, WEBP o GIF.",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="El archivo está vacío")
    if len(content) > MAX_SIGNATURE_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="La imagen supera el máximo de 5 MB")

    original = Path(file.filename or "firma.png").name
    extension = Path(original).suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        extension = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(content_type, ".png")

    stored_name = f"{uuid4().hex}{extension}"
    relative_path = f"signatures/{stored_name}"
    destination = settings.uploads_dir / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)

    return SignatureImageOut(
        name=original,
        size=len(content),
        path=relative_path,
        url=f"/uploads/{relative_path}",
        content_type=content_type or "application/octet-stream",
    )


@router.get("/", response_model=list[MailAccountsOut])
def list_mail_accounts(
    active_only: bool = False,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[MailAccounts]:
    query = db.query(MailAccounts)
    if active_only:
        query = query.filter(MailAccounts.is_active.is_(True))
    return query.order_by(MailAccounts.is_default.desc(), MailAccounts.name.asc(), MailAccounts.id.asc()).all()


@router.get("/{account_id}", response_model=MailAccountsOut)
def get_mail_account(
    account_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> MailAccounts:
    account = db.query(MailAccounts).filter(MailAccounts.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Mail account not found")
    return account


@router.post("/", response_model=MailAccountsOut, status_code=status.HTTP_201_CREATED)
def create_mail_account(
    payload: MailAccountsCreate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> MailAccounts:
    email = payload.email.lower().strip()
    exists = db.query(MailAccounts).filter(MailAccounts.email == email).first()
    if exists:
        raise HTTPException(status_code=400, detail="Email already exists")

    if payload.is_default:
        _clear_other_defaults(db)

    account = MailAccounts(
        name=payload.name.strip(),
        email=email,
        password=payload.password,
        smtp_host=payload.smtp_host.strip(),
        smtp_port=str(payload.smtp_port).strip() or "587",
        imap_host=payload.imap_host.strip(),
        imap_port=str(payload.imap_port).strip() or "993",
        use_ssl=payload.use_ssl,
        is_active=payload.is_active,
        is_default=payload.is_default,
        signature=payload.signature or "",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.put("/{account_id}", response_model=MailAccountsOut)
def update_mail_account(
    account_id: int,
    payload: MailAccountsUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> MailAccounts:
    account = db.query(MailAccounts).filter(MailAccounts.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Mail account not found")

    data = payload.model_dump(exclude_unset=True)

    if "email" in data and data["email"]:
        email = str(data["email"]).lower().strip()
        exists = (
            db.query(MailAccounts)
            .filter(MailAccounts.email == email, MailAccounts.id != account_id)
            .first()
        )
        if exists:
            raise HTTPException(status_code=400, detail="Email already exists")
        data["email"] = email

    for key in ("name", "smtp_host", "imap_host", "smtp_port", "imap_port"):
        if key in data and data[key] is not None:
            data[key] = str(data[key]).strip()

    if "signature" in data and data["signature"] is None:
        data["signature"] = ""

    if data.get("is_default"):
        _clear_other_defaults(db, keep_id=account_id)

    for key, value in data.items():
        setattr(account, key, value)

    db.commit()
    db.refresh(account)
    return account


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mail_account(
    account_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> None:
    account = db.query(MailAccounts).filter(MailAccounts.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Mail account not found")
    db.delete(account)
    db.commit()
