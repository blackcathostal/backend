from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.models.complaints import Complaints
from app.models.mail_accounts import MailAccounts
from app.schemas.complaints import COMPLAINT_TYPES, RELATIONS, STATUSES
from app.services.mailer import MailSendError, send_html_email

IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp"}
IMAGE_MAX = 5 * 1024 * 1024
OTHER_MAX = 20 * 1024 * 1024
ALLOWED_OTHER = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "audio/mpeg",
    "audio/mp4",
    "video/mp4",
    "text/plain",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def type_label(value: str) -> str:
    return COMPLAINT_TYPES.get(value, value or "—")


def relation_label(value: str) -> str:
    return RELATIONS.get(value, value or "—")


def status_label(value: str) -> str:
    return STATUSES.get(value, value or "—")


def new_tracking_code() -> str:
    return "BCH-" + secrets.token_hex(3).upper()


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("56"):
        return digits
    if len(digits) == 9 and digits.startswith("9"):
        return "56" + digits
    if len(digits) == 8:
        return "569" + digits
    return digits


def whatsapp_url(phone: str, text: str) -> str:
    digits = normalize_phone(phone)
    if not digits:
        return ""
    return f"https://wa.me/{digits}?text={quote(text)}"


def _pick_mail_account(db: Session) -> MailAccounts | None:
    accounts = (
        db.query(MailAccounts)
        .filter(MailAccounts.is_active.is_(True))
        .order_by(MailAccounts.is_default.desc(), MailAccounts.id.asc())
        .all()
    )
    return accounts[0] if accounts else None


def officer_inbox() -> str:
    return (settings.complaint_inbox_email or settings.admin_email or "").strip()


def display_name(row: Complaints) -> str:
    if row.is_anonymous:
        return "Anónima"
    return (row.full_name or "").strip() or "Sin nombre"


def serialize(row: Complaints, *, include_whatsapp: bool = False) -> dict[str, Any]:
    data = {
        "id": row.id,
        "tracking_code": row.tracking_code,
        "complaint_type": row.complaint_type,
        "complaint_type_label": type_label(row.complaint_type),
        "complaint_type_other": row.complaint_type_other or "",
        "relation": row.relation,
        "relation_label": relation_label(row.relation),
        "relation_other": row.relation_other or "",
        "location": row.location or "",
        "happened_at": row.happened_at or "",
        "description": row.description,
        "accused_name": row.accused_name,
        "is_anonymous": bool(row.is_anonymous),
        "full_name": "" if row.is_anonymous else (row.full_name or ""),
        "email": row.email or "",
        "phone": row.phone or "",
        "document_type": "" if row.is_anonymous else (row.document_type or ""),
        "document_number": "" if row.is_anonymous else (row.document_number or ""),
        "data_consent": bool(row.data_consent),
        "data_consent_accepted_at": row.data_consent_accepted_at,
        "status": row.status,
        "status_label": status_label(row.status),
        "investigator_name": row.investigator_name or "",
        "measures": row.measures or "",
        "attachments": row.attachments or [],
        "events": row.events or [],
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "whatsapp_url": "",
    }
    if include_whatsapp and row.phone:
        data["whatsapp_url"] = whatsapp_url(
            row.phone,
            update_whatsapp_text(row, note=""),
        )
    return data


def last_public_message(row: Complaints) -> str:
    for event in reversed(row.events or []):
        note = str(event.get("public_message") or event.get("note") or "").strip()
        if note:
            return note
    return (
        "Tu denuncia fue recibida. En un máximo de 3 días hábiles te informaremos "
        "si se investiga internamente o se deriva a la Dirección del Trabajo."
    )


def receipt_email_html(row: Complaints) -> str:
    return f"""
    <p>Recibimos tu denuncia en el Canal de Denuncias de Black Cat Hostal.</p>
    <p><strong>Código de seguimiento:</strong> {escape(row.tracking_code)}</p>
    <p><strong>Estado:</strong> {escape(status_label(row.status))}</p>
    <p>Guarda este código. Puedes consultar el avance en
    {escape(settings.public_site_url.rstrip('/'))}/denuncias/seguimiento</p>
    <p>Según el procedimiento interno (Ley Karin / Código del Trabajo):</p>
    <ol>
      <li>Recepción confidencial (solo la persona designada por la empresa ve el contenido).</li>
      <li>Dentro de <strong>3 días hábiles</strong> se te informará si habrá investigación interna
          o derivación a la Dirección del Trabajo, y las medidas de resguardo.</li>
      <li>Si hay investigación interna, se designa un investigador, se oye a ambas partes
          y el plazo de investigación es de <strong>30 días</strong>.</li>
      <li>Te avisaremos por este correo (y WhatsApp si lo dejaste) cada vez que cambie el estado.</li>
    </ol>
    <p>Si los hechos constituyen un delito, también puedes acudir a Carabineros, PDI o fiscalía.
    Independiente de este canal, puedes denunciar en la Dirección del Trabajo.</p>
    """


def officer_email_html(row: Complaints) -> str:
    accused = escape(row.accused_name)
    desc = escape(row.description).replace("\n", "<br />")
    who = escape(display_name(row))
    kind = escape(type_label(row.complaint_type))
    if row.complaint_type_other:
        kind += f" ({escape(row.complaint_type_other)})"
    return f"""
    <h2>Nueva denuncia — {escape(row.tracking_code)}</h2>
    <p>Ingresó una denuncia al canal interno. Debes:</p>
    <ol>
      <li>Revisar los antecedentes en el admin (Denuncias).</li>
      <li>Adoptar medidas de resguardo si corresponde.</li>
      <li>En <strong>3 días hábiles</strong> decidir investigación interna o derivación a la DT
          e informar a la persona denunciante.</li>
      <li>Si investigas internamente, avisar a la DT (portal Mi DT) y designar investigador.</li>
    </ol>
    <p><strong>Tipo:</strong> {kind}<br />
    <strong>Relación:</strong> {escape(relation_label(row.relation))}<br />
    <strong>Denunciante:</strong> {who}<br />
    <strong>Persona denunciada:</strong> {accused}<br />
    <strong>Dónde:</strong> {escape(row.location or "—")}<br />
    <strong>Cuándo:</strong> {escape(row.happened_at or "—")}<br />
    <strong>Autorización de datos:</strong> {"Sí" if row.data_consent else "No"}<br />
    <strong>Aceptado el:</strong> {escape(
        row.data_consent_accepted_at.astimezone().strftime("%d/%m/%Y %H:%M:%S")
        if row.data_consent_accepted_at
        else "—"
    )}</p>
    <p><strong>Hechos:</strong><br />{desc}</p>
    """


def update_email_html(row: Complaints, note: str) -> str:
    extra = f"<p>{escape(note).replace(chr(10), '<br />')}</p>" if note else ""
    investigator = ""
    if row.investigator_name:
        investigator = f"<p><strong>Persona a cargo:</strong> {escape(row.investigator_name)}</p>"
    measures = ""
    if row.measures:
        measures = f"<p><strong>Medidas de resguardo:</strong> {escape(row.measures)}</p>"
    return f"""
    <p>Actualización de tu denuncia <strong>{escape(row.tracking_code)}</strong>.</p>
    <p><strong>Nuevo estado:</strong> {escape(status_label(row.status))}</p>
    {investigator}
    {measures}
    {extra}
    <p>Puedes ver el estado en
    {escape(settings.public_site_url.rstrip('/'))}/denuncias/seguimiento</p>
    """


def update_whatsapp_text(row: Complaints, note: str) -> str:
    parts = [
        f"Black Cat Hostal — Canal de Denuncias",
        f"Código {row.tracking_code}",
        f"Estado: {status_label(row.status)}",
    ]
    if row.investigator_name:
        parts.append(f"A cargo: {row.investigator_name}")
    if note:
        parts.append(note.strip())
    parts.append("Este canal es confidencial. No respondas datos sensibles por este medio si no quieres.")
    return "\n".join(parts)


def send_mail(db: Session, *, to_email: str, subject: str, html_body: str) -> None:
    if not to_email:
        return
    account = _pick_mail_account(db)
    if not account:
        raise HTTPException(
            status_code=503,
            detail="No hay cuenta de correo SMTP activa para enviar avisos del canal.",
        )
    try:
        send_html_email(account, to_email=to_email, subject=subject, html_body=html_body)
    except MailSendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def notify_new_complaint(db: Session, row: Complaints) -> None:
    inbox = officer_inbox()
    if inbox:
        send_mail(
            db,
            to_email=inbox,
            subject=f"[Denuncia {row.tracking_code}] Nueva denuncia — Black Cat Hostal",
            html_body=officer_email_html(row),
        )
    if row.email:
        send_mail(
            db,
            to_email=row.email,
            subject=f"Recibimos tu denuncia {row.tracking_code} — Black Cat Hostal",
            html_body=receipt_email_html(row),
        )


def notify_update(db: Session, row: Complaints, note: str, *, email: bool, whatsapp: bool) -> str:
    if email and row.email:
        send_mail(
            db,
            to_email=row.email,
            subject=f"Actualización denuncia {row.tracking_code} — Black Cat Hostal",
            html_body=update_email_html(row, note),
        )
    if whatsapp:
        return whatsapp_url(row.phone, update_whatsapp_text(row, note))
    return whatsapp_url(row.phone, update_whatsapp_text(row, note)) if row.phone else ""


async def save_uploads(code: str, files: list[UploadFile] | None) -> list[dict[str, Any]]:
    saved: list[dict[str, Any]] = []
    for upload in files or []:
        filename = (upload.filename or "").strip()
        if not filename:
            continue
        content = await upload.read()
        content_type = (upload.content_type or "application/octet-stream").lower()
        is_image = content_type in IMAGE_TYPES or filename.lower().endswith(
            (".jpg", ".jpeg", ".png", ".gif", ".webp")
        )
        limit = IMAGE_MAX if is_image else OTHER_MAX
        if len(content) > limit:
            raise HTTPException(
                status_code=400,
                detail=f"{filename} supera el máximo ({'5 MB imagen' if is_image else '20 MB'}).",
            )
        if not is_image and content_type not in ALLOWED_OTHER and not filename.lower().endswith(
            (".pdf", ".doc", ".docx", ".txt", ".mp3", ".mp4")
        ):
            raise HTTPException(status_code=400, detail=f"Formato no permitido: {filename}")

        safe_name = Path(filename).name.replace("..", "")
        stored = f"{uuid4().hex}_{safe_name}"
        folder = settings.uploads_dir / "complaints" / code
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / stored
        dest.write_bytes(content)
        url = f"/uploads/complaints/{code}/{stored}"
        saved.append(
            {
                "name": safe_name,
                "path": url,
                "url": url,
                "size": len(content),
                "content_type": content_type,
            }
        )
    return saved


def append_event(row: Complaints, *, actor: str, status: str, note: str, public_message: str) -> None:
    events = list(row.events or [])
    events.append(
        {
            "at": _now_iso(),
            "actor": actor,
            "status": status,
            "note": note,
            "public_message": public_message,
        }
    )
    row.events = events
    flag_modified(row, "events")
