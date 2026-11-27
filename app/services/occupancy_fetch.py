"""Fetch occupancy reports from FNS Informes (ocupación mensual + habitaciones/pasajeros)."""

from __future__ import annotations

import json
import logging
import re
from calendar import monthrange
from datetime import date
from html import unescape
from typing import Any

from app.services.extranet_web_client import ExtranetWebError
from app.services.tax_documents_fetch import TaxDocumentsFetchClient

logger = logging.getLogger(__name__)

_ACTION_MONTHLY = "ocupacionmensual"
_ACTION_DAILY = "habitaciones.personas.dia"


def _strip_html(text: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(text or ""))).strip()


def _parse_pct(value: object) -> float | None:
    text = _strip_html(value).replace("%", "").replace(",", ".").strip()
    if not text or text in {"&nbsp;", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_int(value: object) -> int:
    text = _strip_html(value).replace(".", "").replace(",", "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _parse_room_type_label(label: object) -> tuple[str, int | None]:
    text = _strip_html(label).replace("&nbsp;", " ").strip()
    match = re.match(r"^(.*)\((\d+)\s*\)\s*$", text)
    if not match:
        return text, None
    return match.group(1).strip(), int(match.group(2))


def _parse_cl_date(value: object) -> date | None:
    text = _strip_html(value)
    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", text)
    if not match:
        return None
    day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _table_rows(html: str) -> list[list[str]]:
    tables = re.findall(r"<table[^>]*>([\s\S]*?)</table>", html, flags=re.I)
    if not tables:
        return []
    table = max(tables, key=len)
    rows: list[list[str]] = []
    for row in re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", table, flags=re.I):
        cells = [
            _strip_html(cell).replace("&nbsp;", "").strip()
            for cell in re.findall(r"<t[hd][^>]*>([\s\S]*?)</t[hd]>", row, flags=re.I)
        ]
        if any(cells):
            rows.append(cells)
    return rows


def _json_tablas_rows(html: str) -> tuple[list[str], list[dict[str, Any]]]:
    match = re.search(
        r'name=["\']json_tablas["\']\s+value=["\']([^"\']+)["\']',
        html,
        flags=re.I,
    )
    if not match:
        return [], []
    try:
        payload = json.loads(unescape(match.group(1)).replace("&quot;", '"'))
    except (TypeError, ValueError, json.JSONDecodeError):
        return [], []
    block = payload[0] if isinstance(payload, list) else payload
    if not isinstance(block, dict):
        return [], []
    cabecera = block.get("cabecera") or {}
    headers_raw = cabecera.get("1") if isinstance(cabecera, dict) else None
    headers: list[str] = []
    if isinstance(headers_raw, dict):
        headers = [
            str(headers_raw[key])
            for key in sorted(headers_raw.keys(), key=lambda item: int(item) if str(item).isdigit() else 0)
        ]
    elif isinstance(headers_raw, list):
        headers = [str(item) for item in headers_raw]

    body = block.get("cuerpo") or {}
    rows: list[dict[str, Any]] = []
    if isinstance(body, dict):
        items = [
            body[key]
            for key in sorted(body.keys(), key=lambda item: int(item) if str(item).isdigit() else 0)
        ]
    elif isinstance(body, list):
        items = body
    else:
        items = []
    for item in items:
        if isinstance(item, dict):
            if headers:
                row = {
                    headers[i]: item.get(str(i + 1), item.get(i + 1))
                    for i in range(len(headers))
                }
            else:
                row = {str(k): v for k, v in item.items()}
            rows.append(row)
        elif isinstance(item, list) and headers:
            rows.append(
                {
                    headers[i]: item[i] if i < len(item) else None
                    for i in range(len(headers))
                }
            )
    return headers, rows


def _parse_monthly_occupancy(html: str, *, year: int, month: int) -> dict[str, Any]:
    """
    Parse FNS ocupacionmensual HTML table.
    Columns: room type, then one % per calendar day, optional trailing totals.
    """
    rows = _table_rows(html)
    if len(rows) < 3:
        return {"room_types": [], "daily_total_pct": {}, "total_rooms": None}

    day_headers = rows[0]
    # Skip weekday row if present (D L M …).
    data_start = 2 if len(rows) > 2 and all(len(cell) <= 2 for cell in rows[1][1:8]) else 1

    day_indexes: list[tuple[int, int]] = []
    for idx, header in enumerate(day_headers):
        if idx == 0:
            continue
        try:
            day_num = int(str(header).strip())
        except ValueError:
            continue
        if 1 <= day_num <= 31:
            day_indexes.append((idx, day_num))

    room_types: list[dict[str, Any]] = []
    daily_total_pct: dict[str, float] = {}
    total_rooms: int | None = None

    for row in rows[data_start:]:
        if not row:
            continue
        label, capacity = _parse_room_type_label(row[0])
        daily: dict[str, float] = {}
        for col_idx, day_num in day_indexes:
            if col_idx >= len(row):
                continue
            pct = _parse_pct(row[col_idx])
            if pct is None:
                continue
            key = f"{year:04d}-{month:02d}-{day_num:02d}"
            daily[key] = pct
        values = list(daily.values())
        avg = round(sum(values) / len(values), 2) if values else None
        entry = {
            "room_type": label,
            "capacity": capacity,
            "daily_pct": daily,
            "average_pct": avg,
            "is_total": label.lower().startswith("total"),
        }
        if entry["is_total"]:
            total_rooms = capacity
            daily_total_pct = daily
        else:
            # Skip empty fictitious room types with zero capacity.
            if capacity == 0 and not any(abs(v) > 0.01 for v in values):
                continue
            room_types.append(entry)

    return {
        "room_types": room_types,
        "daily_total_pct": daily_total_pct,
        "total_rooms": total_rooms,
    }


def _row_get(row: dict[str, Any], *candidates: str) -> object:
    by_norm = {
        re.sub(r"[^a-z0-9]+", "", _strip_html(key).lower()): value
        for key, value in row.items()
    }
    for candidate in candidates:
        key = re.sub(r"[^a-z0-9]+", "", candidate.lower())
        if key in by_norm:
            return by_norm[key]
        for existing, value in by_norm.items():
            if key and (key in existing or existing in key):
                return value
    return None


def _parse_daily_activity(html: str) -> list[dict[str, Any]]:
    headers, json_rows = _json_tablas_rows(html)
    days: list[dict[str, Any]] = []
    if json_rows:
        for row in json_rows:
            issued = _parse_cl_date(
                _row_get(row, "Fecha")
                or (row.get(headers[1]) if len(headers) > 1 else None)
            )
            if issued is None:
                continue
            days.append(
                {
                    "date": issued.isoformat(),
                    "weekday": _strip_html(_row_get(row, "Día", "Dia") or ""),
                    "rooms_occupied": _parse_int(_row_get(row, "Habitaciones")),
                    "guests": _parse_int(_row_get(row, "Pasajeros")),
                    "hosts": _parse_int(_row_get(row, "Huéspedes", "Huespedes")),
                    "check_in": _parse_int(_row_get(row, "Check-in", "Check in", "Checkin")),
                    "check_out": _parse_int(
                        _row_get(row, "Check-out", "Check out", "Checkout")
                    ),
                }
            )
        if days:
            return days

    # HTML table fallback.
    for row in _table_rows(html):
        if not row or _strip_html(row[0]).lower() in {"día", "dia"}:
            continue
        if len(row) < 4:
            continue
        issued = _parse_cl_date(row[1] if len(row) > 1 else "")
        if issued is None:
            continue
        days.append(
            {
                "date": issued.isoformat(),
                "weekday": _strip_html(row[0]),
                "rooms_occupied": _parse_int(row[2]),
                "guests": _parse_int(row[3]),
                "hosts": _parse_int(row[4]) if len(row) > 4 else 0,
                "check_in": _parse_int(row[5]) if len(row) > 5 else 0,
                "check_out": _parse_int(row[6]) if len(row) > 6 else 0,
            }
        )
    return days


def fetch_occupancy_report(
    *,
    period_year: int,
    period_month: int,
    day: int | None = None,
) -> dict[str, Any]:
    """
    Pull FNS Informes occupancy for one calendar month.
    Optional day (1–31) filters the returned daily series.
    """
    if period_month < 1 or period_month > 12:
        raise ExtranetWebError("Mes inválido")
    last_day = monthrange(period_year, period_month)[1]
    if day is not None and (day < 1 or day > last_day):
        raise ExtranetWebError("Día inválido para el periodo")

    date_from = date(period_year, period_month, 1)
    date_to = date(period_year, period_month, last_day)
    client = TaxDocumentsFetchClient()
    try:
        monthly_html = client._post_report(
            module="Informes",
            action=_ACTION_MONTHLY,
            date_from=date_from,
            date_to=date_to,
        )
        daily_html = client._post_report(
            module="Informes",
            action=_ACTION_DAILY,
            date_from=date_from,
            date_to=date_to,
        )
    finally:
        client.close()

    monthly = _parse_monthly_occupancy(
        monthly_html, year=period_year, month=period_month
    )
    activity = _parse_daily_activity(daily_html)
    daily_pct = dict(monthly.get("daily_total_pct") or {})

    days: list[dict[str, Any]] = []
    for item in activity:
        key = str(item.get("date") or "")
        pct = daily_pct.get(key)
        row = {**item, "occupancy_pct": pct}
        if day is not None and not key.endswith(f"-{day:02d}"):
            continue
        days.append(row)

    # If activity parse failed, still expose occupancy % days.
    if not days and daily_pct:
        for key, pct in sorted(daily_pct.items()):
            if day is not None and not key.endswith(f"-{day:02d}"):
                continue
            days.append(
                {
                    "date": key,
                    "weekday": "",
                    "rooms_occupied": 0,
                    "guests": 0,
                    "hosts": 0,
                    "check_in": 0,
                    "check_out": 0,
                    "occupancy_pct": pct,
                }
            )

    room_types = list(monthly.get("room_types") or [])
    if day is not None:
        day_key = f"{period_year:04d}-{period_month:02d}-{day:02d}"
        filtered_types = []
        for room in room_types:
            pct_map = dict(room.get("daily_pct") or {})
            if day_key not in pct_map:
                continue
            filtered_types.append(
                {
                    **room,
                    "daily_pct": {day_key: pct_map[day_key]},
                    "average_pct": pct_map[day_key],
                }
            )
        room_types = filtered_types

    pct_values = [
        float(row["occupancy_pct"])
        for row in days
        if row.get("occupancy_pct") is not None
    ]
    month_avg = round(sum(pct_values) / len(pct_values), 2) if pct_values else None

    return {
        "period": f"{period_year:04d}-{period_month:02d}",
        "day": f"{period_year:04d}-{period_month:02d}-{day:02d}" if day else None,
        "total_rooms": monthly.get("total_rooms"),
        "month_avg_occupancy_pct": month_avg,
        "days": days,
        "room_types": room_types,
        "source": "fns_informes_occupancy",
    }
