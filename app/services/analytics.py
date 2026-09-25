from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from app.core.database import engine
from app.models.visitor_analytics import VisitorEvents, VisitorSessions
from app.services.geoip import (
    browser_from_ua,
    device_from_ua,
    hash_ip,
    is_bot,
    lookup_geo,
)

logger = logging.getLogger(__name__)

SKIP_PREFIXES = ("/denuncias",)
ALLOWED_TYPES = {"pageview", "click", "heartbeat", "pageleave"}
LIVE_WINDOW = timedelta(seconds=45)
_schema_ready = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clip(value: str, size: int) -> str:
    return (value or "").strip()[:size]


def ensure_analytics_schema() -> None:
    """Create analytics tables/columns if missing (safe to call often)."""
    global _schema_ready
    if _schema_ready:
        return
    try:
        VisitorSessions.__table__.create(bind=engine, checkfirst=True)
        VisitorEvents.__table__.create(bind=engine, checkfirst=True)
        with engine.begin() as conn:

            def column_exists(table_name: str, column_name: str) -> bool:
                return bool(
                    conn.execute(
                        text(
                            """
                            SELECT COUNT(*)
                            FROM information_schema.COLUMNS
                            WHERE TABLE_SCHEMA = DATABASE()
                              AND TABLE_NAME = :table_name
                              AND COLUMN_NAME = :column_name
                            """
                        ),
                        {"table_name": table_name, "column_name": column_name},
                    ).scalar()
                )

            if not column_exists("visitor_events", "duration_seconds"):
                conn.execute(
                    text(
                        "ALTER TABLE visitor_events "
                        "ADD COLUMN duration_seconds INT NOT NULL DEFAULT 0"
                    )
                )
            if not column_exists("visitor_sessions", "ended_at"):
                conn.execute(text("ALTER TABLE visitor_sessions ADD COLUMN ended_at DATETIME NULL"))
        _schema_ready = True
    except Exception:
        logger.exception("Could not ensure analytics schema")
        _schema_ready = True


def should_skip_path(path: str) -> bool:
    clean = (path or "").split("?")[0].lower()
    return any(clean.startswith(prefix) for prefix in SKIP_PREFIXES)


def ingest_events(
    db: Session,
    *,
    visitor_id: str,
    session_id: str,
    timezone_name: str,
    locale: str,
    referrer: str,
    screen_w: int,
    screen_h: int,
    user_agent: str,
    ip: str,
    events: list[dict],
) -> bool:
    if is_bot(user_agent):
        return False

    visitor_id = _clip(visitor_id, 36)
    session_id = _clip(session_id, 36)
    if not visitor_id or not session_id:
        return False

    usable = [
        event
        for event in events
        if (event.get("type") or "") in ALLOWED_TYPES
        and not should_skip_path(event.get("path") or "")
    ]
    if not usable:
        return False

    now = _now()
    session = (
        db.query(VisitorSessions)
        .filter(VisitorSessions.session_id == session_id)
        .first()
    )
    first_path = _clip(next((item.get("path") or "" for item in usable), "/"), 400)

    if session is None:
        geo = lookup_geo(ip, timezone_name)
        session = VisitorSessions(
            visitor_id=visitor_id,
            session_id=session_id,
            country_code=geo["country_code"],
            country_name=geo["country_name"],
            region=geo["region"],
            city=geo["city"],
            timezone=_clip(timezone_name, 64),
            locale=_clip(locale, 16),
            device=device_from_ua(user_agent),
            browser=browser_from_ua(user_agent),
            user_agent=_clip(user_agent, 400),
            ip_hash=hash_ip(ip),
            landing_path=first_path or "/",
            current_path=first_path or "/",
            referrer=_clip(referrer, 500),
            screen_w=max(0, min(int(screen_w or 0), 10000)),
            screen_h=max(0, min(int(screen_h or 0), 10000)),
            started_at=now,
            last_seen_at=now,
        )
        db.add(session)
        db.flush()

    pageviews = 0
    clicks = 0
    last_path = session.current_path
    rows: list[VisitorEvents] = []
    marked_exit = False
    still_active = False

    for item in usable[:80]:
        event_type = item.get("type") or ""
        path = _clip(item.get("path") or last_path or "/", 400) or "/"
        duration = max(0, min(int(item.get("duration_seconds") or 0), 86_400))
        label = _clip(item.get("label") or "", 160)
        if event_type == "pageview":
            pageviews += 1
            last_path = path
            still_active = True
        elif event_type == "click":
            clicks += 1
            still_active = True
        elif event_type == "heartbeat":
            still_active = True
        elif event_type == "pageleave":
            last_path = path
            if label == "exit":
                marked_exit = True
        if event_type == "heartbeat":
            continue
        rows.append(
            VisitorEvents(
                visitor_id=visitor_id,
                session_id=session_id,
                event_type=event_type,
                path=path,
                title=_clip(item.get("title") or "", 200),
                label=label,
                href=_clip(item.get("href") or "", 500),
                element=_clip(item.get("element") or "", 200),
                x_pct=max(0.0, min(float(item.get("x_pct") or 0), 100.0)),
                y_pct=max(0.0, min(float(item.get("y_pct") or 0), 100.0)),
                duration_seconds=duration,
                created_at=now,
            )
        )

    if rows:
        db.add_all(rows)

    session.last_seen_at = now
    if still_active:
        session.ended_at = None
    elif marked_exit:
        session.ended_at = now
    started = _as_utc(session.started_at) or now
    wall_clock = max(0, int((now - started).total_seconds()))
    try:
        page_time = (
            db.query(func.coalesce(func.sum(VisitorEvents.duration_seconds), 0))
            .filter(
                VisitorEvents.session_id == session_id,
                VisitorEvents.event_type == "pageleave",
            )
            .scalar()
            or 0
        )
    except (OperationalError, ProgrammingError):
        db.rollback()
        page_time = 0
    session.duration_seconds = max(wall_clock, int(page_time), int(session.duration_seconds or 0))
    session.page_count = int(session.page_count or 0) + pageviews
    session.click_count = int(session.click_count or 0) + clicks
    if last_path:
        session.current_path = last_path
    if timezone_name and not session.timezone:
        session.timezone = _clip(timezone_name, 64)
    db.commit()
    return True


def _since(days: int) -> datetime:
    return _now() - timedelta(days=max(1, min(int(days or 7), 90)))


def _avg_duration(rows: list[VisitorSessions]) -> int:
    if not rows:
        return 0
    total = sum(max(0, int(row.duration_seconds or 0)) for row in rows)
    return int(total / len(rows))


def summary(db: Session, days: int = 7) -> dict:
    ensure_analytics_schema()
    since = _since(days)
    start_today = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    live_cutoff = _now() - LIVE_WINDOW

    try:
        period = (
            db.query(VisitorSessions)
            .filter(VisitorSessions.started_at >= since)
            .all()
        )
        live_now = (
            db.query(func.count(VisitorSessions.id))
            .filter(
                VisitorSessions.last_seen_at >= live_cutoff,
                VisitorSessions.ended_at.is_(None),
            )
            .scalar()
            or 0
        )
        page_rows = (
            db.query(
                VisitorEvents.path,
                VisitorEvents.event_type,
                VisitorEvents.visitor_id,
                VisitorEvents.duration_seconds,
            )
            .filter(
                VisitorEvents.created_at >= since,
                VisitorEvents.event_type.in_(("pageview", "click", "pageleave")),
            )
            .all()
        )
    except (OperationalError, ProgrammingError):
        logger.exception("Analytics summary query failed")
        db.rollback()
        return {
            "live_now": 0,
            "visits_today": 0,
            "unique_today": 0,
            "visits_period": 0,
            "unique_period": 0,
            "avg_duration": 0,
            "clicks_period": 0,
            "countries": [],
            "pages": [],
            "devices": [],
            "referrers": [],
        }

    today = [
        row
        for row in period
        if (_as_utc(row.started_at) or since) >= start_today
    ]

    countries_map: dict[str, dict] = {}
    pages_map: dict[str, dict] = defaultdict(
        lambda: {"path": "", "views": 0, "visitors": set(), "clicks": 0, "durations": []}
    )
    devices_map: dict[str, int] = defaultdict(int)
    referrers_map: dict[str, int] = defaultdict(int)

    for row in period:
        code = row.country_code or ""
        name = row.country_name or "Desconocido"
        bucket = countries_map.setdefault(
            code or name,
            {
                "country_code": code,
                "country_name": name,
                "visits": 0,
                "visitors": set(),
                "duration": 0,
                "clicks": 0,
            },
        )
        bucket["visits"] += 1
        bucket["visitors"].add(row.visitor_id)
        bucket["duration"] += max(0, int(row.duration_seconds or 0))
        bucket["clicks"] += int(row.click_count or 0)
        devices_map[row.device or "otro"] += 1
        ref = (row.referrer or "").strip()
        if ref and not ref.startswith(("https://blackcathostal.com", "http://localhost", "http://127.")):
            referrers_map[ref[:180]] += 1
        else:
            referrers_map["Directo"] += 1

    for path, event_type, visitor_id, duration_seconds in page_rows:
        key = path or "/"
        item = pages_map[key]
        item["path"] = key
        item["visitors"].add(visitor_id)
        if event_type == "pageview":
            item["views"] += 1
        elif event_type == "click":
            item["clicks"] += 1
        elif event_type == "pageleave":
            item["durations"].append(max(0, int(duration_seconds or 0)))

    countries = sorted(
        [
            {
                "country_code": item["country_code"],
                "country_name": item["country_name"],
                "visits": item["visits"],
                "unique_visitors": len(item["visitors"]),
                "avg_duration": int(item["duration"] / item["visits"]) if item["visits"] else 0,
                "clicks": item["clicks"],
            }
            for item in countries_map.values()
        ],
        key=lambda item: item["visits"],
        reverse=True,
    )
    pages = sorted(
        [
            {
                "path": item["path"],
                "views": item["views"],
                "unique_visitors": len(item["visitors"]),
                "clicks": item["clicks"],
                "avg_duration": int(sum(item["durations"]) / len(item["durations"]))
                if item["durations"]
                else 0,
            }
            for item in pages_map.values()
            if item["views"] or item["clicks"]
        ],
        key=lambda item: item["views"] or item["clicks"],
        reverse=True,
    )[:40]
    devices = sorted(
        [{"device": name, "visits": count} for name, count in devices_map.items()],
        key=lambda item: item["visits"],
        reverse=True,
    )
    referrers = sorted(
        [{"referrer": name, "visits": count} for name, count in referrers_map.items()],
        key=lambda item: item["visits"],
        reverse=True,
    )[:20]

    return {
        "live_now": int(live_now),
        "visits_today": len(today),
        "unique_today": len({row.visitor_id for row in today}),
        "visits_period": len(period),
        "unique_period": len({row.visitor_id for row in period}),
        "avg_duration": _avg_duration(period),
        "clicks_period": sum(int(row.click_count or 0) for row in period),
        "countries": countries,
        "pages": pages,
        "devices": devices,
        "referrers": referrers,
    }


def list_sessions(
    db: Session,
    *,
    days: int = 7,
    country: str = "",
    limit: int = 80,
    offset: int = 0,
) -> tuple[list[VisitorSessions], int]:
    ensure_analytics_schema()
    since = _since(days)
    try:
        query = db.query(VisitorSessions).filter(VisitorSessions.started_at >= since)
        if country:
            query = query.filter(VisitorSessions.country_code == country.upper())
        total = query.count()
        rows = (
            query.order_by(VisitorSessions.last_seen_at.desc())
            .offset(max(0, offset))
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return rows, total
    except (OperationalError, ProgrammingError):
        logger.exception("Analytics sessions query failed")
        db.rollback()
        return [], 0


def serialize_session(row: VisitorSessions) -> dict:
    live_cutoff = _now() - LIVE_WINDOW
    last_seen = _as_utc(row.last_seen_at)
    ended = _as_utc(getattr(row, "ended_at", None))
    return {
        "id": row.id,
        "session_id": row.session_id,
        "visitor_id": row.visitor_id,
        "country_code": row.country_code,
        "country_name": row.country_name,
        "city": row.city,
        "region": row.region,
        "device": row.device,
        "browser": row.browser,
        "landing_path": row.landing_path,
        "current_path": row.current_path,
        "page_count": row.page_count,
        "click_count": row.click_count,
        "duration_seconds": row.duration_seconds,
        "started_at": row.started_at,
        "last_seen_at": row.last_seen_at,
        "live": bool(last_seen and last_seen >= live_cutoff and ended is None),
    }


def _build_page_stays(events: list[VisitorEvents]) -> list[dict]:
    stays: list[dict] = []
    open_pages: dict[str, dict] = {}

    for event in events:
        if event.event_type == "pageview":
            open_pages[event.path or "/"] = {
                "path": event.path or "/",
                "title": event.title or "",
                "entered_at": event.created_at,
                "left_at": None,
                "duration_seconds": 0,
            }
        elif event.event_type == "pageleave":
            path = event.path or "/"
            current = open_pages.pop(path, None)
            if current is None:
                current = {
                    "path": path,
                    "title": event.title or "",
                    "entered_at": None,
                    "left_at": event.created_at,
                    "duration_seconds": int(getattr(event, "duration_seconds", 0) or 0),
                }
            else:
                current["left_at"] = event.created_at
                current["duration_seconds"] = int(getattr(event, "duration_seconds", 0) or 0)
                if event.title:
                    current["title"] = event.title
            stays.append(current)

    for leftover in open_pages.values():
        stays.append(leftover)

    stays.sort(key=lambda item: item.get("entered_at") or item.get("left_at") or _now())
    return stays


def session_detail(db: Session, session_id: str) -> dict | None:
    ensure_analytics_schema()
    try:
        row = (
            db.query(VisitorSessions)
            .filter(VisitorSessions.session_id == session_id)
            .first()
        )
        if not row:
            return None
        events = (
            db.query(VisitorEvents)
            .filter(VisitorEvents.session_id == session_id)
            .order_by(VisitorEvents.created_at.asc(), VisitorEvents.id.asc())
            .all()
        )
    except (OperationalError, ProgrammingError):
        logger.exception("Analytics session detail failed")
        db.rollback()
        return None
    return {
        "session": serialize_session(row),
        "events": events,
        "page_stays": _build_page_stays(events),
    }


def clicks_for_path(db: Session, path: str, days: int = 7, limit: int = 400) -> dict:
    ensure_analytics_schema()
    since = _since(days)
    wanted = (path or "").strip() or "/"
    try:
        query = db.query(VisitorEvents).filter(
            VisitorEvents.created_at >= since,
            VisitorEvents.event_type == "click",
        )
        if wanted != "*":
            query = query.filter(VisitorEvents.path == wanted)
        rows = query.order_by(VisitorEvents.created_at.desc()).limit(max(1, min(limit, 800))).all()
        session_ids = {row.session_id for row in rows}
        countries = {}
        if session_ids:
            sessions = (
                db.query(VisitorSessions.session_id, VisitorSessions.country_name)
                .filter(VisitorSessions.session_id.in_(session_ids))
                .all()
            )
            countries = {item.session_id: item.country_name for item in sessions}
    except (OperationalError, ProgrammingError):
        logger.exception("Analytics clicks query failed")
        db.rollback()
        return {"path": wanted, "total": 0, "points": [], "top_targets": []}

    targets: dict[str, int] = defaultdict(int)
    points = []
    for row in rows:
        label = row.label or row.element or row.path
        targets[label] += 1
        points.append(
            {
                "path": row.path,
                "label": row.label,
                "href": row.href,
                "element": row.element,
                "x_pct": row.x_pct,
                "y_pct": row.y_pct,
                "country_name": countries.get(row.session_id, ""),
                "session_id": row.session_id,
                "created_at": row.created_at,
            }
        )
    top_targets = [
        {"label": label, "clicks": count}
        for label, count in sorted(targets.items(), key=lambda item: item[1], reverse=True)[:20]
    ]
    return {
        "path": wanted,
        "total": len(points),
        "points": points,
        "top_targets": top_targets,
    }
