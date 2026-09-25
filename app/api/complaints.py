from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.complaints import Complaints
from app.models.users import Users
from app.schemas.complaints import (
    COMPLAINT_TYPES,
    RELATIONS,
    STATUSES,
    ComplaintOut,
    ComplaintPublicOut,
    ComplaintSubmitOut,
    ComplaintUpdate,
)
from app.services import complaints as svc
from app.services.recaptcha import client_ip, verify_recaptcha

router = APIRouter(prefix="/complaints", tags=["complaints"])


def _get_or_404(db: Session, complaint_id: int) -> Complaints:
    row = db.query(Complaints).filter(Complaints.id == complaint_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Denuncia no encontrada")
    return row


def _truthy(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "si", "sí", "yes", "on"}


@router.post("/", response_model=ComplaintSubmitOut, status_code=status.HTTP_201_CREATED)
async def submit_complaint(
    request: Request,
    complaint_type: str = Form(...),
    relation: str = Form(...),
    description: str = Form(...),
    accused_name: str = Form(...),
    is_anonymous: str = Form("false"),
    complaint_type_other: str = Form(""),
    relation_other: str = Form(""),
    location: str = Form(""),
    happened_at: str = Form(""),
    full_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    document_type: str = Form(""),
    document_number: str = Form(""),
    data_consent: str = Form("false"),
    recaptcha_token: str = Form(""),
    files: list[UploadFile] | None = File(None),
    db: Session = Depends(get_db),
) -> ComplaintSubmitOut:
    verify_recaptcha(recaptcha_token, "complaint", client_ip(request))
    anonymous = _truthy(is_anonymous)
    if not _truthy(data_consent):
        raise HTTPException(
            status_code=400,
            detail="Debes autorizar el uso de tus datos personales.",
        )
    if complaint_type not in COMPLAINT_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de denuncia no válido")
    if relation not in RELATIONS:
        raise HTTPException(status_code=400, detail="Relación con la empresa no válida")
    if complaint_type == "otro" and not complaint_type_other.strip():
        raise HTTPException(status_code=400, detail="Especifica el otro tipo de denuncia")
    if relation == "otro" and not relation_other.strip():
        raise HTTPException(status_code=400, detail="Especifica la otra relación con la empresa")
    if len(description.strip()) < 10:
        raise HTTPException(status_code=400, detail="Describe el hecho con más detalle")
    if not accused_name.strip():
        raise HTTPException(status_code=400, detail="Indica la persona objeto de la denuncia")
    if not anonymous and not full_name.strip():
        raise HTTPException(status_code=400, detail="El nombre y apellido son obligatorios")

    code = svc.new_tracking_code()
    while db.query(Complaints).filter(Complaints.tracking_code == code).first():
        code = svc.new_tracking_code()

    consent_at = datetime.now(timezone.utc)
    attachments = await svc.save_uploads(code, files)
    row = Complaints(
        tracking_code=code,
        complaint_type=complaint_type,
        complaint_type_other=complaint_type_other.strip(),
        relation=relation,
        relation_other=relation_other.strip(),
        location=location.strip(),
        happened_at=happened_at.strip(),
        description=description.strip(),
        accused_name=accused_name.strip(),
        is_anonymous=anonymous,
        full_name="" if anonymous else full_name.strip(),
        email=email.strip().lower(),
        phone=svc.normalize_phone(phone),
        document_type="" if anonymous else document_type.strip(),
        document_number="" if anonymous else document_number.strip(),
        data_consent=True,
        data_consent_accepted_at=consent_at,
        status="received",
        attachments=attachments,
        events=[],
    )
    svc.append_event(
        row,
        actor="sistema",
        status="received",
        note="Denuncia ingresada por el canal público.",
        public_message=(
            "Tu denuncia fue recibida. En un máximo de 3 días hábiles te informaremos "
            "si se investiga internamente o se deriva a la Dirección del Trabajo."
        ),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    try:
        svc.notify_new_complaint(db, row)
    except HTTPException:
        # The complaint is stored even if mail fails; admin still sees it.
        pass

    return ComplaintSubmitOut(
        tracking_code=row.tracking_code,
        status_label=svc.status_label(row.status),
        message=(
            "Denuncia enviada. Solo la persona designada por la empresa podrá verla. "
            f"Código de seguimiento: {row.tracking_code}"
        ),
    )


@router.get("/public/{code}", response_model=ComplaintPublicOut)
def track_complaint(code: str, db: Session = Depends(get_db)) -> ComplaintPublicOut:
    row = db.query(Complaints).filter(Complaints.tracking_code == code.strip().upper()).first()
    if not row:
        raise HTTPException(status_code=404, detail="No encontramos una denuncia con ese código")
    return ComplaintPublicOut(
        tracking_code=row.tracking_code,
        status=row.status,
        status_label=svc.status_label(row.status),
        created_at=row.created_at,
        last_message=svc.last_public_message(row),
        investigator_name=row.investigator_name or "",
    )


@router.get("/", response_model=list[ComplaintOut])
def list_complaints(
    status_filter: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[ComplaintOut]:
    query = db.query(Complaints).order_by(Complaints.created_at.desc())
    if status_filter:
        query = query.filter(Complaints.status == status_filter)
    return [ComplaintOut(**svc.serialize(row, include_whatsapp=True)) for row in query.all()]


@router.get("/{complaint_id}", response_model=ComplaintOut)
def get_complaint(
    complaint_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> ComplaintOut:
    row = _get_or_404(db, complaint_id)
    return ComplaintOut(**svc.serialize(row, include_whatsapp=True))


@router.patch("/{complaint_id}", response_model=ComplaintOut)
def update_complaint(
    complaint_id: int,
    payload: ComplaintUpdate,
    db: Session = Depends(get_db),
    current: Users = Depends(get_current_user),
) -> ComplaintOut:
    row = _get_or_404(db, complaint_id)
    if payload.status:
        if payload.status not in STATUSES:
            raise HTTPException(status_code=400, detail="Estado no válido")
        row.status = payload.status
    if payload.investigator_name is not None:
        row.investigator_name = payload.investigator_name.strip()
    if payload.measures is not None:
        row.measures = payload.measures.strip()

    public_message = payload.note.strip() or (
        f"El estado de tu denuncia pasó a: {svc.status_label(row.status)}."
    )
    svc.append_event(
        row,
        actor=current.full_name or current.email or "admin",
        status=row.status,
        note=payload.note.strip(),
        public_message=public_message,
    )
    db.commit()
    db.refresh(row)

    whatsapp = ""
    try:
        whatsapp = svc.notify_update(
            db,
            row,
            payload.note.strip(),
            email=payload.notify_email,
            whatsapp=payload.notify_whatsapp,
        )
    except HTTPException:
        whatsapp = svc.whatsapp_url(row.phone, svc.update_whatsapp_text(row, payload.note.strip()))

    data = svc.serialize(row, include_whatsapp=True)
    data["whatsapp_url"] = whatsapp or data.get("whatsapp_url") or ""
    return ComplaintOut(**data)
