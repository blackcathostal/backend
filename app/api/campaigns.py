from datetime import datetime, timezone
import json
import re
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.campaign_deliveries import (
    DELIVERY_STATUS_BOUNCED,
    DELIVERY_STATUS_ERROR,
    DELIVERY_STATUS_OPENED,
    DELIVERY_STATUS_SENT,
    CampaignDeliveries,
)
from app.models.campaigns import Campaigns
from app.models.mail_accounts import MailAccounts
from app.models.users import Users
from app.schemas.campaigns import (
    CampaignAttachment,
    CampaignDeliveriesResult,
    CampaignDeliveryOut,
    CampaignsCreate,
    CampaignsOut,
    CampaignsUpdate,
)
from app.services.mailer import MailSendError, iter_campaign_emails, validate_attachments

# 1x1 transparent GIF
_TRACKING_PIXEL = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00"
    b"\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


def _public_api_base() -> str:
    return str(getattr(settings, "public_api_base_url", "") or "").rstrip("/")


def _inject_open_pixel(html_body: str, tracking_token: str) -> str:
    base = _public_api_base()
    if not base or not tracking_token:
        return html_body
    pixel_url = f"{base}/campaigns/track/{tracking_token}.gif"
    pixel = (
        f'<img src="{pixel_url}" width="1" height="1" alt="" '
        'style="display:block;width:1px;height:1px;border:0;" />'
    )
    body = html_body or ""
    if re.search(r"</body\s*>", body, flags=re.IGNORECASE):
        return re.sub(r"</body\s*>", pixel + "</body>", body, count=1, flags=re.IGNORECASE)
    return f"{body}{pixel}"

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024


def _dump_items(items: list | None) -> list:
    if not items:
        return []
    return [
        item.model_dump(exclude_none=False) if hasattr(item, "model_dump") else dict(item)
        for item in items
    ]


def _normalize_attachments(items: list | None) -> list:
    dumped = _dump_items(items)
    invalid = [
        str(item.get("name") or "adjunto")
        for item in dumped
        if not str(item.get("path") or "").strip()
    ]
    if invalid:
        names = ", ".join(invalid)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Adjunto(s) sin archivo en servidor: {names}. "
                "Súbelos de nuevo en el editor hasta que digan «listo»."
            ),
        )
    return dumped


def _account_snapshot(account: MailAccounts) -> SimpleNamespace:
    return SimpleNamespace(
        name=account.name,
        email=account.email,
        password=account.password,
        smtp_host=account.smtp_host,
        smtp_port=account.smtp_port,
        use_ssl=account.use_ssl,
        signature=getattr(account, "signature", "") or "",
    )


def _safe_filename(name: str | None) -> str:
    raw = Path(name or "adjunto").name
    cleaned = re.sub(r"[^\w.\- ()\[\]]+", "_", raw, flags=re.UNICODE).strip("._ ")
    return cleaned[:180] or "adjunto"


@router.post(
    "/attachments/upload",
    response_model=CampaignAttachment,
    status_code=status.HTTP_201_CREATED,
)
async def upload_campaign_attachment(
    file: UploadFile = File(...),
    _: Users = Depends(get_current_user),
) -> CampaignAttachment:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="El archivo está vacío")
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=400,
            detail="El adjunto supera el máximo de 15 MB",
        )

    original_name = _safe_filename(file.filename)
    extension = Path(original_name).suffix.lower()
    stored_name = f"{uuid4().hex}{extension}"
    relative_path = f"campaigns/{stored_name}"
    destination = settings.uploads_dir / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)

    return CampaignAttachment(
        name=original_name,
        size=len(content),
        path=relative_path,
        url=f"/uploads/{relative_path}",
        content_type=file.content_type or "application/octet-stream",
    )


@router.get("/", response_model=list[CampaignsOut])
def list_campaigns(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[Campaigns]:
    return db.query(Campaigns).order_by(Campaigns.id.desc()).all()


@router.get("/{campaign_id}", response_model=CampaignsOut)
def get_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Campaigns:
    campaign = db.query(Campaigns).filter(Campaigns.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.post("/", response_model=CampaignsOut, status_code=status.HTTP_201_CREATED)
def create_campaign(
    payload: CampaignsCreate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Campaigns:
    campaign = Campaigns(
        name=payload.name.strip(),
        from_email=str(payload.from_email).lower().strip(),
        subject=payload.subject.strip(),
        html_body=payload.html_body,
        status=payload.status.strip() or "Borrador",
        sent=payload.sent or 0,
        recipients=_dump_items(payload.recipients),
        attachments=_normalize_attachments(payload.attachments),
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


@router.put("/{campaign_id}", response_model=CampaignsOut)
def update_campaign(
    campaign_id: int,
    payload: CampaignsUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Campaigns:
    campaign = db.query(Campaigns).filter(Campaigns.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"]:
        data["name"] = data["name"].strip()
    if "subject" in data and data["subject"]:
        data["subject"] = data["subject"].strip()
    if "from_email" in data and data["from_email"]:
        data["from_email"] = str(data["from_email"]).lower().strip()
    if "status" in data and data["status"]:
        data["status"] = data["status"].strip()
    if "recipients" in data and data["recipients"] is not None:
        data["recipients"] = _dump_items(payload.recipients)
    if "attachments" in data and data["attachments"] is not None:
        data["attachments"] = _normalize_attachments(payload.attachments)

    for key, value in data.items():
        setattr(campaign, key, value)

    db.commit()
    db.refresh(campaign)
    return campaign


@router.get("/{campaign_id}/deliveries", response_model=CampaignDeliveriesResult)
def list_campaign_deliveries(
    campaign_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> CampaignDeliveriesResult:
    campaign = db.query(Campaigns).filter(Campaigns.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    rows = (
        db.query(CampaignDeliveries)
        .filter(CampaignDeliveries.campaign_id == campaign_id)
        .order_by(CampaignDeliveries.id.desc())
        .all()
    )
    return CampaignDeliveriesResult(
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        total=len(rows),
        items=[CampaignDeliveryOut.model_validate(row) for row in rows],
    )


@router.get("/track/{token}.gif", include_in_schema=False)
@router.get("/track/{token}", include_in_schema=False)
def track_campaign_open(token: str, db: Session = Depends(get_db)) -> Response:
    clean = (token or "").strip().removesuffix(".gif")
    if not clean:
        return Response(content=_TRACKING_PIXEL, media_type="image/gif")
    row = (
        db.query(CampaignDeliveries)
        .filter(CampaignDeliveries.tracking_token == clean)
        .first()
    )
    if row and row.status in {DELIVERY_STATUS_SENT, DELIVERY_STATUS_OPENED}:
        if row.status != DELIVERY_STATUS_OPENED:
            row.status = DELIVERY_STATUS_OPENED
            row.opened_at = datetime.now(timezone.utc)
            db.add(row)
            db.commit()
    return Response(
        content=_TRACKING_PIXEL,
        media_type="image/gif",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@router.post("/{campaign_id}/send")
def send_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
):
    campaign = db.query(Campaigns).filter(Campaigns.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    recipients = list(campaign.recipients or [])
    if not recipients:
        raise HTTPException(
            status_code=400,
            detail="La campaña no tiene destinatarios. Edítala y selecciona al menos uno.",
        )

    from_email = (campaign.from_email or "").strip().lower()
    account = (
        db.query(MailAccounts)
        .filter(
            MailAccounts.email == from_email,
            MailAccounts.is_active.is_(True),
        )
        .first()
    )
    if not account:
        raise HTTPException(
            status_code=400,
            detail=(
                f'No hay una cuenta activa para "{from_email}". '
                "Configúrala en Configuraciones · Correos y vuelve a intentar."
            ),
        )

    subject = campaign.subject
    html_body = campaign.html_body
    campaign_name = campaign.name
    attachments = list(campaign.attachments or [])
    account_data = _account_snapshot(account)

    try:
        validate_attachments(attachments)
    except MailSendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Fresh delivery rows for this send wave
    prepared: list[dict] = []
    for item in recipients:
        email = str((item or {}).get("email") or "").strip().lower()
        full_name = str((item or {}).get("full_name") or "").strip()
        token = uuid4().hex
        delivery = CampaignDeliveries(
            campaign_id=campaign_id,
            email=email or "",
            full_name=full_name,
            status=DELIVERY_STATUS_ERROR if not email else DELIVERY_STATUS_SENT,
            error_message="" if email else "Sin correo",
            tracking_token=token,
        )
        db.add(delivery)
        db.flush()
        prepared.append(
            {
                **dict(item or {}),
                "email": email,
                "full_name": full_name,
                "delivery_id": delivery.id,
                "tracking_token": token,
            }
        )
    db.commit()

    token_by_delivery = {int(item["delivery_id"]): item["tracking_token"] for item in prepared}

    def html_for_recipient(item: dict) -> str:
        token = str(item.get("tracking_token") or token_by_delivery.get(int(item.get("delivery_id") or 0)) or "")
        return _inject_open_pixel(html_body, token)

    def event_stream():
        for event in iter_campaign_emails(
            account_data,
            subject=subject,
            html_body=html_body,
            recipients=prepared,
            attachments=attachments,
            html_for_recipient=html_for_recipient,
        ):
            if event["type"] == "progress":
                delivery_id = event.get("delivery_id")
                if delivery_id:
                    row = (
                        db.query(CampaignDeliveries)
                        .filter(CampaignDeliveries.id == int(delivery_id))
                        .first()
                    )
                    if row:
                        if event.get("ok"):
                            row.status = DELIVERY_STATUS_SENT
                            row.error_message = ""
                            row.sent_at = datetime.now(timezone.utc)
                        else:
                            kind = event.get("failure_kind") or DELIVERY_STATUS_ERROR
                            row.status = (
                                DELIVERY_STATUS_BOUNCED
                                if kind == DELIVERY_STATUS_BOUNCED
                                else DELIVERY_STATUS_ERROR
                            )
                            row.error_message = str(event.get("failure_error") or "")[:2000]
                        db.add(row)
                        db.commit()

            if event["type"] == "error":
                yield json.dumps(event, ensure_ascii=False) + "\n"
                return

            if event["type"] == "done":
                final_sent = int(event.get("sent") or 0)
                final_failed = int(event.get("failed") or 0)
                if final_sent <= 0:
                    first_error = "No se pudo enviar"
                    failed_items = event.get("failed_items") or []
                    if failed_items:
                        first_error = failed_items[0].get("error") or first_error
                    yield json.dumps(
                        {
                            "type": "error",
                            "message": f"No se envió ningún correo. {first_error}",
                            "total": event.get("total") or 0,
                            "sent": 0,
                            "failed": final_failed,
                            "remaining": event.get("total") or 0,
                            "percent": 0,
                        },
                        ensure_ascii=False,
                    ) + "\n"
                    return

                row = db.query(Campaigns).filter(Campaigns.id == campaign_id).first()
                campaign_payload = None
                if row:
                    row.status = "Enviada" if final_failed == 0 else "Enviada parcial"
                    row.sent = final_sent
                    row.sent_at = datetime.now(timezone.utc)
                    db.commit()
                    db.refresh(row)
                    campaign_payload = CampaignsOut.model_validate(row).model_dump(mode="json")

                payload = {
                    **event,
                    "campaign": campaign_payload,
                    "name": campaign_name,
                }
                yield json.dumps(payload, ensure_ascii=False) + "\n"
                return

            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> None:
    campaign = db.query(Campaigns).filter(Campaigns.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    db.delete(campaign)
    db.commit()
