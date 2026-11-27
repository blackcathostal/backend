"""Persist and read occupancy snapshots in the local DB."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.occupancy_stats import OccupancyDayStats, OccupancyRoomTypeStats
from app.services.extranet_web_client import ExtranetWebError
from app.services.occupancy_fetch import fetch_occupancy_report


def _period_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _parse_iso_date(value: object) -> date | None:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def refresh_occupancy_period(
    db: Session,
    *,
    period_year: int,
    period_month: int,
) -> dict[str, Any]:
    """Pull FNS for the month, replace local rows, return the stored report."""
    payload = fetch_occupancy_report(
        period_year=period_year,
        period_month=period_month,
        day=None,
    )
    period = _period_key(period_year, period_month)
    total_rooms = payload.get("total_rooms")

    db.query(OccupancyDayStats).filter(OccupancyDayStats.period == period).delete(
        synchronize_session=False
    )
    db.query(OccupancyRoomTypeStats).filter(
        OccupancyRoomTypeStats.period == period
    ).delete(synchronize_session=False)

    for item in payload.get("days") or []:
        day_date = _parse_iso_date(item.get("date"))
        if day_date is None:
            continue
        db.add(
            OccupancyDayStats(
                period=period,
                day_date=day_date,
                weekday=str(item.get("weekday") or "")[:20],
                rooms_occupied=int(item.get("rooms_occupied") or 0),
                guests=int(item.get("guests") or 0),
                hosts=int(item.get("hosts") or 0),
                check_in=int(item.get("check_in") or 0),
                check_out=int(item.get("check_out") or 0),
                occupancy_pct=(
                    float(item["occupancy_pct"])
                    if item.get("occupancy_pct") is not None
                    else None
                ),
                total_rooms=int(total_rooms) if total_rooms is not None else None,
            )
        )

    for room in payload.get("room_types") or []:
        room_type = str(room.get("room_type") or "").strip()[:180]
        if not room_type:
            continue
        capacity = room.get("capacity")
        daily = room.get("daily_pct") or {}
        for day_key, pct in daily.items():
            day_date = _parse_iso_date(day_key)
            if day_date is None:
                continue
            db.add(
                OccupancyRoomTypeStats(
                    period=period,
                    day_date=day_date,
                    room_type=room_type,
                    capacity=int(capacity) if capacity is not None else None,
                    occupancy_pct=float(pct) if pct is not None else None,
                )
            )

    db.commit()
    return load_occupancy_report(
        db,
        period_year=period_year,
        period_month=period_month,
        day=None,
        room_type=None,
    )


def load_occupancy_report(
    db: Session,
    *,
    period_year: int,
    period_month: int,
    day: int | None = None,
    room_type: str | None = None,
) -> dict[str, Any]:
    """Build the occupancy report from local DB rows (no FNS call)."""
    if period_month < 1 or period_month > 12:
        raise ExtranetWebError("Mes inválido")
    last_day = monthrange(period_year, period_month)[1]
    if day is not None and (day < 1 or day > last_day):
        raise ExtranetWebError("Día inválido para el periodo")

    period = _period_key(period_year, period_month)
    day_query = db.query(OccupancyDayStats).filter(OccupancyDayStats.period == period)
    room_query = db.query(OccupancyRoomTypeStats).filter(
        OccupancyRoomTypeStats.period == period
    )
    if day is not None:
        target = date(period_year, period_month, day)
        day_query = day_query.filter(OccupancyDayStats.day_date == target)
        room_query = room_query.filter(OccupancyRoomTypeStats.day_date == target)

    day_rows = day_query.order_by(OccupancyDayStats.day_date.asc()).all()
    room_rows = room_query.order_by(
        OccupancyRoomTypeStats.room_type.asc(),
        OccupancyRoomTypeStats.day_date.asc(),
    ).all()

    room_type_filter = str(room_type or "").strip()
    available_room_types = sorted(
        {
            str(value or "").strip()
            for (value,) in db.query(OccupancyRoomTypeStats.room_type)
            .filter(OccupancyRoomTypeStats.period == period)
            .distinct()
            .all()
            if str(value or "").strip()
        }
    )

    if room_type_filter:
        room_rows = [
            row for row in room_rows if str(row.room_type or "").strip() == room_type_filter
        ]

    days = [
        {
            "date": row.day_date.isoformat(),
            "weekday": row.weekday or "",
            "rooms_occupied": int(row.rooms_occupied or 0),
            "guests": int(row.guests or 0),
            "hosts": int(row.hosts or 0),
            "check_in": int(row.check_in or 0),
            "check_out": int(row.check_out or 0),
            "occupancy_pct": (
                float(row.occupancy_pct) if row.occupancy_pct is not None else None
            ),
        }
        for row in day_rows
    ]

    by_type: dict[str, dict[str, Any]] = {}
    for row in room_rows:
        name = str(row.room_type or "").strip()
        if not name:
            continue
        bucket = by_type.setdefault(
            name,
            {
                "room_type": name,
                "capacity": int(row.capacity) if row.capacity is not None else None,
                "daily_pct": {},
                "average_pct": None,
                "is_total": False,
            },
        )
        if row.capacity is not None:
            bucket["capacity"] = int(row.capacity)
        if row.occupancy_pct is not None:
            bucket["daily_pct"][row.day_date.isoformat()] = float(row.occupancy_pct)

    room_types: list[dict[str, Any]] = []
    for bucket in by_type.values():
        values = list(bucket["daily_pct"].values())
        bucket["average_pct"] = (
            round(sum(values) / len(values), 2) if values else None
        )
        room_types.append(bucket)

    pct_values = [
        float(item["occupancy_pct"])
        for item in days
        if item.get("occupancy_pct") is not None
    ]
    month_avg = round(sum(pct_values) / len(pct_values), 2) if pct_values else None
    total_rooms = next(
        (int(row.total_rooms) for row in day_rows if row.total_rooms is not None),
        None,
    )
    refreshed_at: datetime | None = None
    if day_rows:
        refreshed_at = max(
            (row.updated_at for row in day_rows if row.updated_at is not None),
            default=None,
        )

    return {
        "period": period,
        "day": (
            f"{period_year:04d}-{period_month:02d}-{day:02d}" if day is not None else None
        ),
        "room_type": room_type_filter or None,
        "available_room_types": available_room_types,
        "total_rooms": total_rooms,
        "month_avg_occupancy_pct": month_avg,
        "days": days,
        "room_types": room_types,
        "refreshed_at": refreshed_at.isoformat() if refreshed_at else None,
        "source": "occupancy_stats_db",
        "has_data": bool(day_rows or room_rows),
    }
