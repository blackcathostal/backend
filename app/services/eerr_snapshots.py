"""Persist and load monthly EERR snapshots for consolidated (DB-only) reads."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.eerr_month_snapshots import EerrMonthSnapshots
from app.schemas.reports import EerrReportOut

logger = logging.getLogger(__name__)


def save_eerr_month_snapshot(db: Session, report: EerrReportOut) -> None:
    """Upsert the monthly EERR payload for consolidated reuse."""
    period = str(report.period or "").strip()
    if not period:
        return
    payload = report.model_dump()
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    row = (
        db.query(EerrMonthSnapshots)
        .filter(EerrMonthSnapshots.period == period)
        .one_or_none()
    )
    if row is None:
        row = EerrMonthSnapshots(
            period=period,
            payload_json=raw,
            source=str(report.source or ""),
        )
        db.add(row)
    else:
        row.payload_json = raw
        row.source = str(report.source or "")
    db.commit()


def load_eerr_month_snapshot(db: Session, period: str) -> EerrReportOut | None:
    """Return cached monthly EERR for period, or None if never loaded."""
    key = str(period or "").strip()
    if not key:
        return None
    row = (
        db.query(EerrMonthSnapshots)
        .filter(EerrMonthSnapshots.period == key)
        .one_or_none()
    )
    if row is None or not str(row.payload_json or "").strip():
        return None
    try:
        data: dict[str, Any] = json.loads(row.payload_json)
        return EerrReportOut.model_validate(data)
    except Exception as exc:
        logger.warning("EERR snapshot invalid for %s: %s", key, exc)
        return None


def load_eerr_month_snapshots(
    db: Session, periods: list[str]
) -> dict[str, EerrReportOut]:
    """Load all available snapshots for the given YYYY-MM keys."""
    keys = [str(period or "").strip() for period in periods if str(period or "").strip()]
    if not keys:
        return {}
    rows = (
        db.query(EerrMonthSnapshots)
        .filter(EerrMonthSnapshots.period.in_(keys))
        .all()
    )
    out: dict[str, EerrReportOut] = {}
    for row in rows:
        try:
            data = json.loads(row.payload_json or "{}")
            report = EerrReportOut.model_validate(data)
            out[str(row.period)] = report
        except Exception as exc:
            logger.warning("EERR snapshot invalid for %s: %s", row.period, exc)
    return out


def empty_eerr_month_report(period: str) -> EerrReportOut:
    """Placeholder month when the monthly EERR was never opened/cached."""
    return EerrReportOut(
        period=str(period or ""),
        section_title="Ingresos",
        rows=[],
        grand_total=0,
        grand_net=0,
        grand_tax=0,
        expense_section_title="Egresos",
        expense_rows=[],
        expenses_total=0,
        result_total=0,
        source="eerr_month_snapshots",
    )
