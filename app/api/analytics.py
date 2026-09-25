from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.users import Users
from app.schemas.analytics import (
    AnalyticsCollectIn,
    AnalyticsCollectOut,
    AnalyticsSummaryOut,
    ClicksOut,
    SessionDetailOut,
    SessionListOut,
)
from app.services.analytics import (
    clicks_for_path,
    ingest_events,
    list_sessions,
    serialize_session,
    session_detail,
    summary,
)
from app.services.recaptcha import client_ip

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.post("/collect", response_model=AnalyticsCollectOut)
def collect_analytics(
    payload: AnalyticsCollectIn,
    request: Request,
    db: Session = Depends(get_db),
) -> AnalyticsCollectOut:
    accepted = ingest_events(
        db,
        visitor_id=payload.visitor_id,
        session_id=payload.session_id,
        timezone_name=payload.timezone,
        locale=payload.locale,
        referrer=payload.referrer,
        screen_w=payload.screen_w,
        screen_h=payload.screen_h,
        user_agent=request.headers.get("user-agent") or "",
        ip=client_ip(request),
        events=[event.model_dump() for event in payload.events],
    )
    return AnalyticsCollectOut(ok=True, skipped=not accepted)


@router.get("/summary", response_model=AnalyticsSummaryOut)
def analytics_summary(
    days: int = 7,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> dict:
    return summary(db, days=days)


@router.get("/sessions", response_model=SessionListOut)
def analytics_sessions(
    days: int = 7,
    country: str = "",
    limit: int = 80,
    offset: int = 0,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> dict:
    rows, total = list_sessions(
        db, days=days, country=country, limit=limit, offset=offset
    )
    return {"items": [serialize_session(row) for row in rows], "total": total}


@router.get("/sessions/{session_id}", response_model=SessionDetailOut)
def analytics_session_detail(
    session_id: str,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> dict:
    data = session_detail(db, session_id)
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sesión no encontrada")
    return data


@router.get("/clicks", response_model=ClicksOut)
def analytics_clicks(
    path: str = "/",
    days: int = 7,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> dict:
    return clicks_for_path(db, path=path, days=days)
