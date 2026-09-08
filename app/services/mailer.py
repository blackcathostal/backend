from __future__ import annotations

import mimetypes
import re
import smtplib
from contextlib import contextmanager
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4
from urllib.parse import urlparse

from app.core.config import settings
from app.models.mail_accounts import MailAccounts

IMG_SRC_RE = re.compile(
    r'(<img\b[^>]*?\bsrc=["\'])([^"\']+)(["\'][^>]*>)',
    re.IGNORECASE | re.DOTALL,
)


class MailSendError(Exception):
    """Raised when SMTP send fails for configuration or connection reasons."""


def _smtp_port(account: MailAccounts) -> int:
    try:
        return int(str(account.smtp_port).strip() or "0")
    except ValueError as exc:
        raise MailSendError(f"Puerto SMTP inválido: {account.smtp_port}") from exc


def _resolve_attachment_path(item: dict[str, Any]) -> Path | None:
    raw_path = str(item.get("path") or "").strip().replace("\\", "/")
    raw_url = str(item.get("url") or "").strip().replace("\\", "/")

    relative = ""
    if raw_path:
        relative = raw_path.lstrip("/")
        if relative.startswith("uploads/"):
            relative = relative.removeprefix("uploads/")
    elif raw_url.startswith("/uploads/"):
        relative = raw_url.removeprefix("/uploads/").lstrip("/")
    elif raw_url.startswith("uploads/"):
        relative = raw_url.removeprefix("uploads/")

    if not relative:
        return None

    # Only allow files under uploads/campaigns for safety.
    if not relative.startswith("campaigns/"):
        return None

    file_path = (settings.uploads_dir / relative).resolve()
    uploads_root = settings.uploads_dir.resolve()
    try:
        file_path.relative_to(uploads_root)
    except ValueError:
        return None
    if not file_path.is_file():
        return None
    return file_path


def validate_attachments(attachments: list[dict[str, Any]] | None) -> list[Path]:
    """Return resolved files; raise if any declared attachment cannot be loaded."""
    items = [item for item in (attachments or []) if isinstance(item, dict)]
    if not items:
        return []

    resolved: list[Path] = []
    missing: list[str] = []
    for item in items:
        name = str(item.get("name") or "adjunto").strip() or "adjunto"
        file_path = _resolve_attachment_path(item)
        if file_path is None:
            missing.append(name)
        else:
            resolved.append(file_path)

    if missing:
        names = ", ".join(missing)
        raise MailSendError(
            f"Adjunto(s) no disponibles en el servidor: {names}. "
            "Edita la campaña, quita el adjunto y súbelo de nuevo hasta que diga «listo»."
        )
    return resolved


def _attach_files(message: MIMEMultipart, attachments: list[dict[str, Any]] | None) -> None:
    items = [item for item in (attachments or []) if isinstance(item, dict)]
    validate_attachments(items)

    for item in items:
        file_path = _resolve_attachment_path(item)
        if file_path is None:
            continue

        filename = str(item.get("name") or file_path.name).strip() or file_path.name
        content_type = str(item.get("content_type") or "").strip()
        if not content_type:
            guessed, _ = mimetypes.guess_type(filename)
            content_type = guessed or "application/octet-stream"

        maintype, _, subtype = content_type.partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"

        part = MIMEBase(maintype, subtype)
        part.set_payload(file_path.read_bytes())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        message.attach(part)


def _signature_is_empty(signature: str | None) -> bool:
    sig = (signature or "").strip()
    if not sig:
        return True
    if re.search(r"<img\b", sig, re.IGNORECASE):
        return False
    plain = (
        sig.replace("<br>", " ")
        .replace("<br/>", " ")
        .replace("<br />", " ")
        .replace("&nbsp;", " ")
    )
    plain = re.sub(r"<[^>]+>", " ", plain)
    return not plain.strip()


def _append_signature(html_body: str, signature: str | None) -> str:
    body = html_body or ""
    if _signature_is_empty(signature):
        return body
    sig = (signature or "").strip()
    # Keep signature images side-by-side in email clients.
    sig = re.sub(
        r'<div class="signature-images"[^>]*>',
        '<div class="signature-images" style="display:block;font-size:0;line-height:0;margin:0.35em 0;">',
        sig,
        flags=re.IGNORECASE,
    )
    return (
        f"{body}"
        f'<div class="mail-signature" style="margin-top:1.5em;padding-top:1em;'
        f'border-top:1px solid #dddddd;">{sig}</div>'
    )


def _resolve_upload_url_to_path(src: str) -> Path | None:
    raw = (src or "").strip()
    if not raw or raw.startswith("data:") or raw.startswith("cid:"):
        return None

    parsed = urlparse(raw)
    path = parsed.path if parsed.scheme or parsed.netloc else raw
    path = path.replace("\\", "/")
    if "/uploads/" in path:
        relative = path.split("/uploads/", 1)[1].lstrip("/")
    elif path.startswith("uploads/"):
        relative = path.removeprefix("uploads/")
    elif path.startswith("/uploads/"):
        relative = path.removeprefix("/uploads/").lstrip("/")
    else:
        return None

    if not relative.startswith("signatures/"):
        return None

    file_path = (settings.uploads_dir / relative).resolve()
    uploads_root = settings.uploads_dir.resolve()
    try:
        file_path.relative_to(uploads_root)
    except ValueError:
        return None
    if not file_path.is_file():
        return None
    return file_path


def _embed_inline_images(html: str) -> tuple[str, list[MIMEImage]]:
    """Rewrite local signature image URLs to cid: and return MIME parts."""
    inline_parts: list[MIMEImage] = []

    def replacer(match: re.Match[str]) -> str:
        prefix, src, suffix = match.group(1), match.group(2), match.group(3)
        file_path = _resolve_upload_url_to_path(src)
        if file_path is None:
            return match.group(0)

        content = file_path.read_bytes()
        guessed, _ = mimetypes.guess_type(file_path.name)
        subtype = "octet-stream"
        if guessed and guessed.startswith("image/"):
            subtype = guessed.split("/", 1)[1] or "octet-stream"

        cid = f"{uuid4().hex}@blackcat"
        part = MIMEImage(content, _subtype=subtype)
        part.add_header("Content-ID", f"<{cid}>")
        part.add_header("Content-Disposition", "inline", filename=file_path.name)
        inline_parts.append(part)
        return f"{prefix}cid:{cid}{suffix}"

    rewritten = IMG_SRC_RE.sub(replacer, html or "")
    return rewritten, inline_parts


def _build_message(
    account: MailAccounts,
    *,
    to_email: str,
    subject: str,
    html_body: str,
    reply_to: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> MIMEMultipart:
    message = MIMEMultipart("mixed")
    message["Subject"] = subject or "(sin asunto)"
    message["From"] = formataddr((account.name or "", account.email))
    message["To"] = to_email
    if reply_to:
        message["Reply-To"] = reply_to

    final_html = _append_signature(html_body, getattr(account, "signature", None))
    html_with_cids, inline_parts = _embed_inline_images(final_html)

    related = MIMEMultipart("related")
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText("Abre este correo en un cliente compatible con HTML.", "plain", "utf-8"))
    alternative.attach(MIMEText(html_with_cids or "", "html", "utf-8"))
    related.attach(alternative)
    for part in inline_parts:
        related.attach(part)
    message.attach(related)
    _attach_files(message, attachments)
    return message


@contextmanager
def open_smtp(account: MailAccounts):
    if not account.smtp_host:
        raise MailSendError("La cuenta no tiene host SMTP configurado")
    if not account.password:
        raise MailSendError("La cuenta no tiene contraseña configurada")

    port = _smtp_port(account) or (465 if account.use_ssl else 587)
    host = account.smtp_host.strip()
    use_ssl = bool(account.use_ssl) or port == 465

    try:
        if use_ssl:
            smtp = smtplib.SMTP_SSL(host, port, timeout=45)
        else:
            smtp = smtplib.SMTP(host, port, timeout=45)
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
        smtp.login(account.email, account.password)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailSendError(
            "Autenticación SMTP fallida. Revisa correo y contraseña en Configuraciones · Correos."
        ) from exc
    except smtplib.SMTPException as exc:
        raise MailSendError(f"Error SMTP: {exc}") from exc
    except OSError as exc:
        raise MailSendError(
            f"No se pudo conectar a {host}:{port}. Revisa host, puerto y SSL."
        ) from exc

    try:
        yield smtp
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


def send_html_email(
    account: MailAccounts,
    *,
    to_email: str,
    subject: str,
    html_body: str,
    reply_to: str = "",
    attachments: list[dict[str, Any]] | None = None,
    smtp: smtplib.SMTP | smtplib.SMTP_SSL | None = None,
) -> None:
    to_email = (to_email or "").strip()
    if not to_email:
        raise MailSendError("Destinatario vacío")

    message = _build_message(
        account,
        to_email=to_email,
        subject=subject,
        html_body=html_body,
        reply_to=reply_to,
        attachments=attachments,
    )

    try:
        if smtp is not None:
            smtp.send_message(message)
            return
        with open_smtp(account) as connection:
            connection.send_message(message)
    except MailSendError:
        raise
    except smtplib.SMTPAuthenticationError as exc:
        raise MailSendError(
            "Autenticación SMTP fallida. Revisa correo y contraseña en Configuraciones · Correos."
        ) from exc
    except smtplib.SMTPException as exc:
        raise MailSendError(f"Error SMTP: {exc}") from exc
    except OSError as exc:
        raise MailSendError(f"Error de conexión SMTP: {exc}") from exc


def iter_campaign_emails(
    account: MailAccounts,
    *,
    subject: str,
    html_body: str,
    recipients: list[dict[str, Any]] | None,
    attachments: list[dict[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    items = recipients or []
    total = len(items)
    sent = 0
    failed: list[dict[str, str]] = []

    yield {"type": "start", "total": total, "sent": 0, "failed": 0, "remaining": total}

    if total == 0:
        yield {
            "type": "done",
            "total": 0,
            "sent": 0,
            "failed": 0,
            "remaining": 0,
            "failed_items": [],
        }
        return

    try:
        with open_smtp(account) as smtp:
            for index, item in enumerate(items, start=1):
                email = str(item.get("email") or "").strip().lower()
                if not email:
                    failed.append({"email": "", "error": "Sin correo"})
                else:
                    try:
                        send_html_email(
                            account,
                            to_email=email,
                            subject=subject,
                            html_body=html_body,
                            attachments=attachments,
                            smtp=smtp,
                        )
                        sent += 1
                    except MailSendError as exc:
                        message = str(exc)
                        if "Autenticación SMTP" in message or "No se pudo conectar" in message:
                            yield {
                                "type": "error",
                                "message": message,
                                "total": total,
                                "sent": sent,
                                "failed": len(failed),
                                "remaining": total - index + 1,
                            }
                            return
                        failed.append({"email": email, "error": message})

                yield {
                    "type": "progress",
                    "total": total,
                    "current": index,
                    "sent": sent,
                    "failed": len(failed),
                    "remaining": max(total - index, 0),
                    "email": email,
                    "percent": int(round((index / total) * 100)),
                }
    except MailSendError as exc:
        yield {
            "type": "error",
            "message": str(exc),
            "total": total,
            "sent": sent,
            "failed": len(failed),
            "remaining": total - sent - len(failed),
        }
        return

    yield {
        "type": "done",
        "total": total,
        "sent": sent,
        "failed": len(failed),
        "remaining": 0,
        "failed_items": failed,
        "percent": 100,
    }


def send_campaign_emails(
    account: MailAccounts,
    *,
    subject: str,
    html_body: str,
    recipients: list[dict[str, Any]] | None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = {
        "sent": 0,
        "failed": [],
        "total": len(recipients or []),
    }
    for event in iter_campaign_emails(
        account,
        subject=subject,
        html_body=html_body,
        recipients=recipients,
        attachments=attachments,
    ):
        if event["type"] == "error":
            raise MailSendError(event["message"])
        if event["type"] == "done":
            result = {
                "sent": event["sent"],
                "failed": event.get("failed_items") or [],
                "total": event["total"],
            }
    return result
