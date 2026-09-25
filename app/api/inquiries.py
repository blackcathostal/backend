from datetime import datetime, timezone
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.contact_inquiries import ContactInquiries
from app.models.mail_accounts import MailAccounts
from app.schemas.inquiries import ContactInquiryCreate, ContactInquiryOut
from app.services.mailer import MailSendError, send_html_email
from app.services.recaptcha import client_ip, verify_recaptcha

router = APIRouter(prefix="/inquiries", tags=["inquiries"])


def _pick_mail_account(db: Session) -> MailAccounts | None:
    accounts = (
        db.query(MailAccounts)
        .filter(MailAccounts.is_active.is_(True))
        .order_by(MailAccounts.is_default.desc(), MailAccounts.id.asc())
        .all()
    )
    if not accounts:
        return None
    for account in accounts:
        email = (account.email or "").lower()
        if "reservas" in email or "blackcathostal" in email:
            return account
    return accounts[0]


def _format_consent_at(value: datetime) -> str:
    local = value.astimezone() if value.tzinfo else value
    return local.strftime("%d/%m/%Y %H:%M:%S")


@router.post("/contact", response_model=ContactInquiryOut, status_code=status.HTTP_201_CREATED)
def submit_contact_inquiry(
    payload: ContactInquiryCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> ContactInquiryOut:
    verify_recaptcha(payload.recaptcha_token, "contact", client_ip(request))
    if not payload.data_consent:
        raise HTTPException(
            status_code=400,
            detail="Debes autorizar el uso de tus datos personales.",
        )

    account = _pick_mail_account(db)
    if not account:
        raise HTTPException(
            status_code=503,
            detail="No hay una cuenta de correo configurada para recibir consultas.",
        )

    accepted_at = datetime.now(timezone.utc)
    row = ContactInquiries(
        name=payload.name.strip(),
        email=str(payload.email).strip().lower(),
        phone=(payload.phone or "").strip(),
        subject=payload.subject.strip(),
        message=payload.message.strip(),
        data_consent=True,
        data_consent_accepted_at=accepted_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    safe_name = escape(payload.name.strip())
    safe_phone = escape((payload.phone or "").strip())
    safe_subject = escape(payload.subject.strip())
    safe_message = escape(payload.message.strip()).replace("\n", "<br />")
    safe_consent_at = escape(_format_consent_at(accepted_at))
    inbox = settings.contact_inbox_email

    html_body = f"""
    <h2>Nuevo mensaje desde el sitio web</h2>
    <p><strong>Nombre:</strong> {safe_name}</p>
    <p><strong>Correo:</strong> {escape(str(payload.email))}</p>
    <p><strong>Teléfono:</strong> {safe_phone or "No indicado"}</p>
    <p><strong>Asunto:</strong> {safe_subject}</p>
    <p><strong>Mensaje:</strong><br />{safe_message}</p>
    <p><strong>Autorización de datos personales:</strong> Sí<br />
    <strong>Fecha de aceptación:</strong> {safe_consent_at}</p>
    """

    try:
        send_html_email(
            account,
            to_email=inbox,
            subject=f"[Black Cat Hostal] {payload.subject.strip()}",
            html_body=html_body,
            reply_to=str(payload.email),
        )
    except MailSendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ContactInquiryOut(message="Mensaje enviado correctamente.")
