from html import escape

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.mail_accounts import MailAccounts
from app.schemas.inquiries import ContactInquiryCreate, ContactInquiryOut
from app.services.mailer import MailSendError, send_html_email

router = APIRouter(prefix="/inquiries", tags=["inquiries"])


def _pick_mail_account(db: Session) -> MailAccounts | None:
    accounts = db.query(MailAccounts).order_by(MailAccounts.id.asc()).all()
    if not accounts:
        return None
    for account in accounts:
        email = (account.email or '').lower()
        if 'reservas' in email or 'blackcathostal' in email:
            return account
    return accounts[0]


@router.post('/contact', response_model=ContactInquiryOut, status_code=status.HTTP_201_CREATED)
def submit_contact_inquiry(payload: ContactInquiryCreate, db: Session = Depends(get_db)) -> ContactInquiryOut:
    account = _pick_mail_account(db)
    if not account:
        raise HTTPException(
            status_code=503,
            detail='No hay una cuenta de correo configurada para recibir consultas.',
        )

    safe_name = escape(payload.name.strip())
    safe_phone = escape((payload.phone or '').strip())
    safe_subject = escape(payload.subject.strip())
    safe_message = escape(payload.message.strip()).replace('\n', '<br />')
    inbox = settings.contact_inbox_email

    html_body = f"""
    <h2>Nuevo mensaje desde el sitio web</h2>
    <p><strong>Nombre:</strong> {safe_name}</p>
    <p><strong>Correo:</strong> {escape(str(payload.email))}</p>
    <p><strong>Teléfono:</strong> {safe_phone or 'No indicado'}</p>
    <p><strong>Asunto:</strong> {safe_subject}</p>
    <p><strong>Mensaje:</strong><br />{safe_message}</p>
    """

    try:
        send_html_email(
            account,
            to_email=inbox,
            subject=f'[Black Cat Hostal] {payload.subject.strip()}',
            html_body=html_body,
        )
    except MailSendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ContactInquiryOut(message='Mensaje enviado correctamente.')
