from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.audits import Audits
from app.models.users import Users
from app.schemas.audits import AuditCreate, AuditOut

router = APIRouter(prefix="/audits", tags=["audits"])


@router.post("/", response_model=AuditOut)
def create_audit(
    payload: AuditCreate,
    db: Session = Depends(get_db),
    current_user: Users = Depends(get_current_user),
) -> AuditOut:
    row = Audits(
        user_id=getattr(current_user, "id", None),
        user_email=str(getattr(current_user, "email", "") or "")[:255],
        event_type=str(payload.event_type or "navigation")[:40],
        action=str(payload.action or "")[:120],
        path=str(payload.path or "")[:500],
        screen=str(payload.screen or "")[:200],
        detail=str(payload.detail or ""),
        duration_ms=max(0, int(payload.duration_ms or 0)),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return AuditOut.model_validate(row)


@router.get("/", response_model=list[AuditOut])
def list_audits(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[AuditOut]:
    rows = db.query(Audits).order_by(Audits.id.desc()).limit(limit).all()
    return [AuditOut.model_validate(row) for row in rows]
