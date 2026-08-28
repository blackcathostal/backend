from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.ai_generation_runs import AiGenerationRuns
from app.models.ai_sources import AiSources
from app.models.ai_usage import AiUsage
from app.models.users import Users
from app.schemas.ai import (
    AiGenerationOut,
    AiSourceCreate,
    AiSourceOut,
    AiSourceUpdate,
    AiUsageOut,
    AiUsageSummary,
)
from app.services.ai_content_generation import generate_and_publish
from app.core.config import settings
from app.services.ai_source_fetcher import SourceFetchError, fetch_source_content, validate_source_url

router = APIRouter(prefix="/ai", tags=["ai"])


@router.get("/sources/", response_model=list[AiSourceOut])
def list_sources(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[AiSources]:
    return db.query(AiSources).order_by(AiSources.priority.desc(), AiSources.id.desc()).all()


@router.post("/sources/", response_model=AiSourceOut, status_code=status.HTTP_201_CREATED)
def create_source(
    payload: AiSourceCreate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> AiSources:
    try:
        url = validate_source_url(payload.url)
    except SourceFetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    source = AiSources(**payload.model_dump(exclude={"url"}), url=url)
    db.add(source)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Ya existe una fuente con esa URL.") from exc
    db.refresh(source)
    return source


@router.post("/sources/{source_id}/test", response_model=AiSourceOut)
async def test_source(
    source_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> AiSources:
    source = db.query(AiSources).filter(AiSources.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Fuente no encontrada.")
    try:
        await fetch_source_content(
            source.url,
            timeout_seconds=settings.deepseek_source_timeout_seconds,
            max_bytes=settings.deepseek_source_max_bytes,
        )
        source.last_status = "ok"
        source.last_error = None
    except Exception as exc:
        source.last_status = "error"
        source.last_error = str(exc)[:1000]
    source.last_checked_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(source)
    return source


@router.get("/sources/{source_id}", response_model=AiSourceOut)
def get_source(
    source_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> AiSources:
    source = db.query(AiSources).filter(AiSources.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Fuente no encontrada.")
    return source


@router.put("/sources/{source_id}", response_model=AiSourceOut)
def update_source(
    source_id: int,
    payload: AiSourceUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> AiSources:
    source = db.query(AiSources).filter(AiSources.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Fuente no encontrada.")
    data = payload.model_dump(exclude_unset=True)
    if "url" in data:
        try:
            data["url"] = validate_source_url(data["url"])
        except SourceFetchError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    for key, value in data.items():
        setattr(source, key, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Ya existe una fuente con esa URL.") from exc
    db.refresh(source)
    return source


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(
    source_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> None:
    source = db.query(AiSources).filter(AiSources.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Fuente no encontrada.")
    db.delete(source)
    db.commit()


@router.get("/usage/report", response_model=AiUsageSummary)
def usage_report(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> AiUsageSummary:
    to_date = datetime.now(timezone.utc)
    from_date = to_date - timedelta(days=days)
    query = db.query(AiUsage).filter(AiUsage.created_at >= from_date)
    entries = query.order_by(AiUsage.created_at.desc(), AiUsage.id.desc()).limit(limit).all()
    values = query.with_entities(
        func.coalesce(func.sum(AiUsage.estimated_cost_usd), 0.0),
        func.coalesce(func.sum(AiUsage.prompt_tokens), 0),
        func.coalesce(func.sum(AiUsage.prompt_cache_hit_tokens), 0),
        func.coalesce(func.sum(AiUsage.prompt_cache_miss_tokens), 0),
        func.coalesce(func.sum(AiUsage.completion_tokens), 0),
        func.coalesce(func.sum(AiUsage.total_tokens), 0),
    ).one()
    runs_query = db.query(AiGenerationRuns).filter(AiGenerationRuns.created_at >= from_date)
    runs = runs_query.count()
    successful_runs = runs_query.filter(AiGenerationRuns.status == "success").count()
    failed_runs = runs_query.filter(AiGenerationRuns.status == "failed").count()
    hit = int(values[2] or 0)
    miss = int(values[3] or 0)
    return AiUsageSummary(
        from_date=from_date,
        to_date=to_date,
        total_cost_usd=float(values[0] or 0.0),
        prompt_tokens=int(values[1] or 0),
        prompt_cache_hit_tokens=hit,
        prompt_cache_miss_tokens=miss,
        completion_tokens=int(values[4] or 0),
        total_tokens=int(values[5] or 0),
        runs=runs,
        successful_runs=successful_runs,
        failed_runs=failed_runs,
        cache_hit_ratio=round(hit / (hit + miss), 4) if hit + miss else 0.0,
        entries=[AiUsageOut.model_validate(item) for item in entries],
    )


@router.post("/content/refresh", response_model=AiGenerationOut)
async def refresh_content(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> AiGenerationOut:
    try:
        result = await generate_and_publish(db)
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return AiGenerationOut(
        message="Artículo generado y publicado correctamente.",
        post_id=result["post"].id,
        run_id=result["run"].id,
        usage=AiUsageOut.model_validate(result["usage"]),
    )
