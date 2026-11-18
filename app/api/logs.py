from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user, get_current_user_optional
from app.models.logs import Logs
from app.models.users import Users
from app.schemas.logs import LogCreate, LogOut

router = APIRouter(prefix="/logs", tags=["logs"])
logger = logging.getLogger(__name__)


def _persist_log(
    db: Session,
    *,
    payload: LogCreate,
    user: Users | None,
) -> Logs:
    row = Logs(
        source=str(payload.source or "frontend")[:40],
        level=str(payload.level or "error")[:20],
        message=str(payload.message or ""),
        stack=str(payload.stack or ""),
        path=str(payload.path or "")[:500],
        user_id=getattr(user, "id", None) if user else None,
        user_email=str(getattr(user, "email", "") or "")[:255] if user else "",
        meta_json=str(payload.meta_json or ""),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.post("/", response_model=LogOut)
def create_log(
    payload: LogCreate,
    db: Session = Depends(get_db),
    current_user: Users | None = Depends(get_current_user_optional),
) -> LogOut:
    row = _persist_log(db, payload=payload, user=current_user)
    return LogOut.model_validate(row)


@router.get("/", response_model=list[LogOut])
def list_logs(
    limit: int = Query(default=100, ge=1, le=500),
    source: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[LogOut]:
    query = db.query(Logs)
    if source:
        query = query.filter(Logs.source == source.strip().lower())
    rows = query.order_by(Logs.id.desc()).limit(limit).all()
    return [LogOut.model_validate(row) for row in rows]


def capture_backend_error(
    db: Session,
    *,
    message: str,
    path: str = "",
    stack: str = "",
    user: Users | None = None,
    meta: dict | None = None,
) -> None:
    try:
        _persist_log(
            db,
            payload=LogCreate(
                source="backend",
                level="error",
                message=message,
                stack=stack,
                path=path,
                meta_json=json.dumps(meta or {}, ensure_ascii=False, default=str),
            ),
            user=user,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to persist backend error log")
