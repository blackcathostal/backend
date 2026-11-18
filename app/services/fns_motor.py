"""FNSrooms Motor API: availability + booking (Basic Auth)."""

from __future__ import annotations

import json
import logging
import threading
import unicodedata
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_BOOKINGS_LOCK = threading.Lock()
_BOOKINGS_FILE = settings.uploads_dir / "cache" / "fns_channel_bookings.json"


class FnsMotorError(Exception):
    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.details = details


def _motor_configured() -> bool:
    return bool(
        settings.fns_channel_user.strip()
        and settings.fns_channel_password.strip()
        and settings.fns_channel_id.strip()
        and settings.fns_hotel_id.strip()
    )


def _motor_url(path: str) -> str:
    base = settings.fns_motor_base_url.rstrip("/")
    # Docs server URL ends with /recieveNotifications/ota/2.1/
    root = f"{base}/recieveNotifications/ota/2.1"
    clean = "/" + str(path or "").lstrip("/")
    if clean.startswith("/recieveNotifications/"):
        return base + clean
    return root + clean


def _auth() -> tuple[str, str]:
    return (settings.fns_channel_user.strip(), settings.fns_channel_password.strip())


def _parse_allowlist(raw: str) -> set[str]:
    return {
        part.strip().lower().lstrip("@")
        for part in str(raw or "").split(",")
        if part.strip()
    }


def booking_allowed(*, channel: str, username: str | None) -> bool:
    """Pilot gate: only listed Instagram/Facebook users can use FNS booking tools."""
    handle = str(username or "").strip().lower().lstrip("@")
    if not handle:
        return False
    ch = str(channel or "instagram").strip().lower()
    if ch in {"instagram", "ig"}:
        return handle in _parse_allowlist(settings.fns_booking_ig_allowlist)
    if ch in {"facebook", "fb", "messenger"}:
        return handle in _parse_allowlist(settings.fns_booking_fb_allowlist)
    return False


def _ms_auth() -> tuple[str, str]:
    user = (settings.fns_ms_user or settings.fns_pms_user or "").strip()
    password = (settings.fns_ms_password or settings.fns_pms_password or "").strip()
    return (user, password)


def _parse_fns_cambio_rate(payload: Any, *, day: str) -> float | None:
    """Parse Maestros_Cambio response: [{ \"YYYY-MM-DD\": \"995.12\" }, ...]."""
    rows: list[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("data", "cambio", "rates", "result"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        else:
            rows = [payload]
    else:
        return None

    preferred: float | None = None
    fallback: float | None = None
    for row in rows:
        if not isinstance(row, dict) or not row:
            continue
        for key, raw in row.items():
            try:
                value = float(str(raw).strip())
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            if str(key)[:10] == day:
                preferred = value
            elif fallback is None:
                fallback = value
    return preferred if preferred is not None else fallback


async def fetch_fns_cambio_usd_clp_rate(*, day: str | None = None) -> dict[str, Any]:
    """FNSrooms Maestros_Cambio: CLP per 1 USD (moneda_src=USD, moneda_dst=CLP)."""
    central = str(settings.fns_central_id or "").strip()
    user, password = _ms_auth()
    if not central or not user or not password:
        raise FnsMotorError(
            "FNS Maestros_Cambio no configurado (FNS_CENTRAL_ID + credenciales MS/PMS)"
        )

    stamp = (day or datetime.now(timezone.utc).strftime("%Y-%m-%d"))[:10]
    body: dict[str, Any] = {
        "central_id": int(central),
        "start_date": stamp,
        "end_date": stamp,
        "moneda_src_id": int(settings.fns_currency_usd_id or 2),
        "moneda_dst_id": int(settings.fns_currency_clp_id or 4),
    }
    pms_id = str(settings.fns_pms_id or "").strip()
    if pms_id:
        body["pms_id"] = int(pms_id)

    base = (settings.fns_ms_base_url or "https://ms.fnsmanager.com").rstrip("/")
    url = f"{base}/v1/maestros/cambio.php"
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        response = await client.post(url, json=body, auth=(user, password))
    if response.status_code >= 400:
        raise FnsMotorError(
            f"FNS Maestros_Cambio HTTP {response.status_code}",
            details=response.text[:500],
        )
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise FnsMotorError("FNS Maestros_Cambio JSON inválido", details=response.text[:500]) from exc

    value = _parse_fns_cambio_rate(payload, day=stamp)
    if value is None or value <= 0:
        raise FnsMotorError("FNS Maestros_Cambio sin tasa usable", details=payload)
    return {
        "ok": True,
        "usd_clp_rate": float(value),
        "source": "fns_maestros_cambio",
        "as_of": stamp,
        "central_id": central,
        "raw": payload,
    }


async def fetch_usd_clp_rate() -> dict[str, Any]:
    """CLP per 1 USD for converting hostel FAQ prices to USD for the guest.

    Prefer FNSrooms Maestros_Cambio (internal rate), then optional env override,
    then public FX feeds as fallback.
    """
    configured = float(settings.fns_usd_clp_rate or 0)
    if configured > 0:
        return {
            "ok": True,
            "usd_clp_rate": configured,
            "source": "env",
            "as_of": None,
        }

    errors: list[str] = []
    try:
        return await fetch_fns_cambio_usd_clp_rate()
    except FnsMotorError as exc:
        errors.append(f"fns_cambio {exc}")
        if getattr(exc, "details", None):
            errors.append(str(exc.details)[:160])

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        # Fallback 1) Chilean observed USD
        try:
            response = await client.get("https://mindicador.cl/api/dolar")
            if response.status_code < 400:
                payload = response.json()
                serie = payload.get("serie") or []
                row = serie[0] if serie else {}
                value = float(row.get("valor") or 0)
                if value > 0:
                    return {
                        "ok": True,
                        "usd_clp_rate": value,
                        "source": "mindicador.cl",
                        "as_of": row.get("fecha"),
                        "unit": str(payload.get("unidad_medida") or "Pesos"),
                        "fallback_of": "fns_maestros_cambio",
                    }
            errors.append(f"mindicador HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"mindicador {exc}")

        # Fallback 2) public USD→CLP
        try:
            response = await client.get("https://open.er-api.com/v6/latest/USD")
            if response.status_code < 400:
                payload = response.json()
                rates = payload.get("rates") or {}
                value = float(rates.get("CLP") or 0)
                if value > 0:
                    return {
                        "ok": True,
                        "usd_clp_rate": value,
                        "source": "open.er-api.com",
                        "as_of": payload.get("time_last_update_utc"),
                        "fallback_of": "fns_maestros_cambio",
                    }
            errors.append(f"er-api HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"er-api {exc}")

        try:
            response = await client.get(
                "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json"
            )
            if response.status_code < 400:
                payload = response.json()
                value = float(((payload.get("usd") or {}).get("clp")) or 0)
                if value > 0:
                    return {
                        "ok": True,
                        "usd_clp_rate": value,
                        "source": "currency-api",
                        "as_of": payload.get("date"),
                        "fallback_of": "fns_maestros_cambio",
                    }
            errors.append(f"currency-api HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"currency-api {exc}")

    raise FnsMotorError(
        "No se pudo obtener el tipo de cambio USD/CLP",
        details="; ".join(errors)[:500],
    )


def clp_to_usd(amount_clp: float | int, usd_clp_rate: float) -> float:
    rate = float(usd_clp_rate or 0)
    if rate <= 0:
        return 0.0
    return round(float(amount_clp) / rate, 2)


def _inventory_units(value: Any) -> int:
    """Parse FNS invcount; anything non-positive is treated as no stock."""
    text = str(value if value is not None else "").strip()
    if not text:
        return 0
    try:
        return max(0, int(float(text)))
    except (TypeError, ValueError):
        return 0


async def search_availability(
    *,
    start_date: str,
    end_date: str,
    currency: str = "USD",
    adults: int = 2,
    children: int = 0,
) -> dict[str, Any]:
    if not _motor_configured():
        raise FnsMotorError("FNS Motor no está configurado en .env")
    start = str(start_date or "").strip()
    end = str(end_date or "").strip()
    if not start or not end:
        raise FnsMotorError("Faltan fechas start_date / end_date (YYYY-MM-DD)")
    body = {
        "auth": {"channel_id": str(settings.fns_channel_id)},
        "params": {
            "lang": "es",
            "hotel_id": str(settings.fns_hotel_id),
            "start_date": start,
            "end_date": end,
            "currency": (currency or "USD").strip().upper() or "USD",
            "hotelData": 1,
            "adults": max(1, int(adults or 2)),
            "children": max(0, int(children or 0)),
        },
    }
    url = _motor_url("/search_availability_json.php")
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, json=body, auth=_auth())
    if response.status_code >= 400:
        raise FnsMotorError(
            f"Motor availability HTTP {response.status_code}",
            details=response.text[:500],
        )
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise FnsMotorError("Respuesta inválida del Motor", details=response.text[:500]) from exc

    hotels = payload.get("hotelAvailabilities") or []
    if not hotels:
        return {
            "ok": True,
            "available": False,
            "start_date": start,
            "end_date": end,
            "currency": body["params"]["currency"],
            "rooms": [],
            "message": "No hay disponibilidad para esas fechas.",
        }

    hotel = hotels[0] if isinstance(hotels[0], dict) else {}
    rooms_out: list[dict[str, Any]] = []
    for room in hotel.get("room_types") or []:
        if not isinstance(room, dict):
            continue
        inventory = _inventory_units(room.get("invcount"))
        # Never expose sold-out room types as bookable.
        if inventory <= 0:
            continue
        rateplans = []
        for plan in room.get("rateplans") or []:
            if not isinstance(plan, dict):
                continue
            rateplans.append(
                {
                    "rate_plan_code": str(plan.get("rateplancode") or plan.get("rate_id") or ""),
                    "rate_name": str(plan.get("RateName") or ""),
                    "occupancy": str(plan.get("occupancy") or ""),
                    "price": plan.get("price"),
                    "currency": plan.get("currency_source")
                    or hotel.get("currency")
                    or body["params"]["currency"],
                    "day_rates": [
                        {
                            "day": str(day.get("day") or ""),
                            "pnight": str(day.get("pnight") or ""),
                        }
                        for day in (plan.get("dayRates") or [])
                        if isinstance(day, dict)
                    ],
                }
            )
        rooms_out.append(
            {
                "room_type_code": str(room.get("code") or room.get("thid") or ""),
                "room_name": str(room.get("RoomName") or ""),
                "inventory": str(inventory),
                "min_price": room.get("minprice"),
                "rate_plans": rateplans,
            }
        )
    return {
        "ok": True,
        "available": bool(rooms_out),
        "hotel_id": str(hotel.get("hotel_id") or settings.fns_hotel_id),
        "hotel_name": str((hotel.get("hotelData") or {}).get("name") or "Black Cat Hostal"),
        "start_date": start,
        "end_date": end,
        "currency": hotel.get("currency") or body["params"]["currency"],
        "rooms": rooms_out,
        "message": (
            f"Hay cupo ({len(rooms_out)} tipos) para {start}→{end}."
            if rooms_out
            else f"No hay disponibilidad para {start}→{end}."
        ),
    }


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_document(value: str | None) -> str:
    """Normalize RUT/passport for exact matching (strip dots, dashes, spaces)."""
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    return "".join(ch for ch in raw if ch.isalnum())


def infer_document_type(document: str, document_type: str = "") -> str:
    explicit = str(document_type or "").strip().upper()
    if explicit in {"RUT", "PASAPORTE", "PASSPORT", "DNI", "CI"}:
        return "RUT" if explicit == "CI" else ("PASAPORTE" if explicit == "PASSPORT" else explicit)
    doc = normalize_document(document)
    # Chilean RUT typically ends with digit or K and is 8–9 chars without punctuation.
    if doc and len(doc) <= 9 and doc[:-1].isdigit() and (doc[-1].isdigit() or doc[-1] == "K"):
        return "RUT"
    return "PASAPORTE" if doc else ""


def document_requires_full_name(document: str, document_type: str = "") -> bool:
    """Passport (and non-RUT IDs) must be paired with full name for safer match."""
    kind = infer_document_type(document, document_type)
    return kind == "PASAPORTE"


async def create_booking(
    *,
    start_date: str,
    end_date: str,
    room_type_code: str,
    rate_plan_code: str,
    amount_after_tax: str | float | int,
    currency: str = "CLP",
    units: int = 1,
    adults: int = 2,
    children: int = 0,
    guest_first_name: str,
    guest_last_name: str,
    guest_document: str = "",
    guest_document_type: str = "",
    guest_email: str = "",
    guest_phone: str = "",
    remarks: str = "",
    day_rates: list[dict[str, str]] | None = None,
    external_id: str | None = None,
) -> dict[str, Any]:
    if not _motor_configured():
        raise FnsMotorError("FNS Motor no está configurado en .env")

    start = str(start_date or "").strip()
    end = str(end_date or "").strip()
    room_code = str(room_type_code or "").strip()
    rate_code = str(rate_plan_code or "").strip()
    first = str(guest_first_name or "").strip()
    last = str(guest_last_name or "").strip()
    document = str(guest_document or "").strip()
    doc_type = infer_document_type(document, guest_document_type)
    if not all([start, end, room_code, rate_code, first, last]):
        raise FnsMotorError(
            "Faltan datos: fechas, habitación, tarifa, nombre y apellido del huésped"
        )

    total = str(amount_after_tax).strip()
    if not total:
        raise FnsMotorError("Falta amount_after_tax")
    # IG/FB bookings are stored in hostel CLP tariffs.
    cur = (currency or "CLP").strip().upper() or "CLP"
    booking_id = str(external_id or "").strip() or f"BC-{uuid.uuid4().hex[:12].upper()}"
    adult_n = max(1, int(adults or 1))
    child_n = max(0, int(children or 0))
    unit_n = max(1, int(units or 1))

    guest_counts: list[dict[str, str]] = [
        {"Count": str(adult_n), "AgeQualifyingCode": "10"},
    ]
    if child_n:
        guest_counts.append({"Count": str(child_n), "AgeQualifyingCode": "8"})

    rate_plan: dict[str, Any] = {"RatePlanCode": rate_code}
    if day_rates:
        rate_plan["dayRates"] = [
            {"day": str(d.get("day") or ""), "pnight": str(d.get("pnight") or "")}
            for d in day_rates
            if str(d.get("day") or "").strip()
        ]

    customer: dict[str, Any] = {
        "PersonName": {
            "GivenName": first,
            "Surname": last,
        },
        # FNS listbooking exposes these as DocumentType + NIF.
        "DocumentType": doc_type,
        "NIF": document,
    }
    if guest_email.strip():
        customer["Email"] = guest_email.strip()
    if guest_phone.strip():
        customer["Telephone"] = {"PhoneNumber": guest_phone.strip()}

    body = {
        "auth": {"channel_id": str(settings.fns_channel_id)},
        "reserva": {
            "TimeStamp": _now_stamp(),
            "ResStatus": "Book",
            "HotelReservations": {
                "HotelReservation": {
                    "CreateDateTime": _now_stamp(),
                    "UniqueID": {"ID": booking_id},
                    "RoomStays": {
                        "RoomStay": [
                            {
                                "RoomTypes": {
                                    "RoomType": {
                                        "NumberOfUnits": str(unit_n),
                                        "RoomTypeCode": room_code,
                                    }
                                },
                                "RatePlans": {"RatePlan": rate_plan},
                                "GuestCounts": {"GuestCount": guest_counts},
                                "ResGuests": {
                                    "ResGuest": {
                                        "Profiles": {
                                            "ProfileInfo": [
                                                {"Profile": {"Customer": customer}}
                                            ]
                                        }
                                    }
                                },
                                "Total": {
                                    "AmountAfterTax": total,
                                    "CurrencyCode": cur,
                                },
                            }
                        ]
                    },
                    "ResGlobalInfo": {
                        "TimeSpan": {"Start": start, "End": end},
                        "Total": {
                            "AmountAfterTax": total,
                            "CurrencyCode": cur,
                        },
                        "BasicPropertyInfo": {
                            "HotelCode": str(settings.fns_hotel_id),
                        },
                        "Remarks": remarks.strip() or "Reserva Instagram/Facebook Black Cat",
                    },
                }
            },
        },
    }

    url = _motor_url("/booking_json.php")
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, json=body, auth=_auth())
    text = response.text
    if response.status_code >= 400:
        raise FnsMotorError(
            f"Motor booking HTTP {response.status_code}",
            details=text[:800],
        )
    try:
        payload = response.json() if text.strip() else {}
    except Exception:
        payload = {"raw": text[:800]}

    logger.info("FNS booking created external_id=%s status=%s", booking_id, response.status_code)
    fns_id = _extract_fns_id(payload)
    result = {
        "ok": True,
        "booking_id": booking_id,
        "fns_id": fns_id,
        "hotel_id": str(settings.fns_hotel_id),
        "start_date": start,
        "end_date": end,
        "room_type_code": room_code,
        "rate_plan_code": rate_code,
        "amount_after_tax": total,
        "currency": cur,
        "guest_first_name": first,
        "guest_last_name": last,
        "guest_document": document,
        "guest_document_type": doc_type,
        "guest_document_normalized": normalize_document(document),
        "guest": f"{first} {last}".strip(),
        "status": "Book",
        "motor_response": payload,
    }
    return result


def _bookings_path() -> Path:
    path = _BOOKINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_booking_index() -> dict[str, Any]:
    path = _bookings_path()
    if not path.exists():
        return {"bookings": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"bookings": []}
    if not isinstance(data, dict):
        return {"bookings": []}
    rows = data.get("bookings")
    if not isinstance(rows, list):
        data["bookings"] = []
    return data


def _save_booking_index(data: dict[str, Any]) -> None:
    path = _bookings_path()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_fns_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    # Common shapes from Motor responses.
    for key in ("IDFNS", "idFNS", "fns_id", "reservation_id"):
        if payload.get(key):
            return str(payload.get(key))
    hotel = payload.get("HotelReservations") or {}
    reservation = hotel.get("HotelReservation")
    if isinstance(reservation, list) and reservation:
        reservation = reservation[0]
    if isinstance(reservation, dict):
        unique = reservation.get("UniqueID") or {}
        if isinstance(unique, dict) and unique.get("IDFNS"):
            return str(unique.get("IDFNS"))
    return ""


def remember_channel_booking(
    *,
    booking: dict[str, Any],
    username: str | None,
    channel: str | None,
) -> dict[str, Any]:
    """Persist IG/FB handle ↔ FNS booking so the agent can detect duplicates."""
    handle = str(username or "").strip().lower().lstrip("@")
    ch = str(channel or "instagram").strip().lower() or "instagram"
    document = str(booking.get("guest_document") or "").strip()
    doc_type = str(booking.get("guest_document_type") or infer_document_type(document))
    row = {
        "booking_id": str(booking.get("booking_id") or ""),
        "fns_id": str(booking.get("fns_id") or ""),
        "channel": ch,
        "username": handle,
        "guest_first_name": str(booking.get("guest_first_name") or ""),
        "guest_last_name": str(booking.get("guest_last_name") or ""),
        "guest_document": document,
        "guest_document_type": doc_type,
        "guest_document_normalized": normalize_document(
            booking.get("guest_document_normalized") or document
        ),
        "start_date": str(booking.get("start_date") or ""),
        "end_date": str(booking.get("end_date") or ""),
        "room_type_code": str(booking.get("room_type_code") or ""),
        "amount_after_tax": str(booking.get("amount_after_tax") or ""),
        "currency": str(booking.get("currency") or "CLP"),
        "status": str(booking.get("status") or "Book"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cancelled_at": None,
    }
    if not row["booking_id"]:
        return row
    with _BOOKINGS_LOCK:
        data = _load_booking_index()
        rows = [r for r in (data.get("bookings") or []) if isinstance(r, dict)]
        rows = [r for r in rows if str(r.get("booking_id") or "") != row["booking_id"]]
        rows.append(row)
        data["bookings"] = rows[-500:]
        _save_booking_index(data)
    return row


def _mark_local_booking_cancelled(booking_id: str) -> None:
    bid = str(booking_id or "").strip()
    if not bid:
        return
    with _BOOKINGS_LOCK:
        data = _load_booking_index()
        changed = False
        for row in data.get("bookings") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("booking_id") or "") != bid:
                continue
            row["status"] = "Cancel"
            row["cancelled_at"] = datetime.now(timezone.utc).isoformat()
            changed = True
        if changed:
            _save_booking_index(data)


def _as_reservation_list(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    hotel = payload.get("HotelReservations") or {}
    if not isinstance(hotel, dict):
        return []
    raw = hotel.get("HotelReservation")
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


def _reservation_customer(reservation: dict[str, Any]) -> dict[str, Any]:
    try:
        stays = reservation.get("RoomStays") or {}
        stay = stays.get("RoomStay")
        if isinstance(stay, list):
            stay = stay[0] if stay else {}
        guests = ((stay or {}).get("ResGuests") or {}).get("ResGuest") or {}
        profiles = ((guests.get("Profiles") or {}).get("ProfileInfo") or [])
        profile = profiles[0] if isinstance(profiles, list) and profiles else profiles
        customer = (((profile or {}).get("Profile") or {}).get("Customer") or {})
        return customer if isinstance(customer, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _reservation_guest_name(reservation: dict[str, Any]) -> tuple[str, str]:
    customer = _reservation_customer(reservation)
    person = customer.get("PersonName") or {}
    return (
        str(person.get("GivenName") or "").strip(),
        str(person.get("Surname") or "").strip(),
    )


def _reservation_document(reservation: dict[str, Any]) -> tuple[str, str]:
    customer = _reservation_customer(reservation)
    document = str(customer.get("NIF") or customer.get("Document") or "").strip()
    doc_type = str(customer.get("DocumentType") or "").strip()
    return document, infer_document_type(document, doc_type)


def _reservation_dates(reservation: dict[str, Any]) -> tuple[str, str]:
    info = reservation.get("ResGlobalInfo") or {}
    span = info.get("TimeSpan") or {}
    return str(span.get("Start") or "")[:10], str(span.get("End") or "")[:10]


def _normalize_booking_row(reservation: dict[str, Any]) -> dict[str, Any]:
    unique = reservation.get("UniqueID") or {}
    if not isinstance(unique, dict):
        unique = {}
    first, last = _reservation_guest_name(reservation)
    document, doc_type = _reservation_document(reservation)
    start, end = _reservation_dates(reservation)
    total = ((reservation.get("ResGlobalInfo") or {}).get("Total") or {})
    status = str(reservation.get("ResStatus") or "").strip() or "Book"
    return {
        "booking_id": str(unique.get("ID") or ""),
        "fns_id": str(unique.get("IDFNS") or ""),
        "status": status,
        "start_date": start,
        "end_date": end,
        "guest_first_name": first,
        "guest_last_name": last,
        "guest_document": document,
        "guest_document_type": doc_type,
        "guest_document_normalized": normalize_document(document),
        "guest": f"{first} {last}".strip(),
        "amount_after_tax": str(total.get("AmountAfterTax") or ""),
        "currency": str(total.get("CurrencyCode") or ""),
        "create_date_time": str(reservation.get("CreateDateTime") or ""),
    }


def _is_active_unused(row: dict[str, Any], *, today: date) -> bool:
    status = str(row.get("status") or "").strip().lower()
    if status in {"cancel", "cancelled", "canceled"}:
        return False
    end_raw = str(row.get("end_date") or row.get("start_date") or "")[:10]
    try:
        end = datetime.strptime(end_raw, "%Y-%m-%d").date()
    except ValueError:
        return status in {"book", "pending", "modified", ""}
    # Still usable if checkout is today or in the future.
    return end >= today


async def list_bookings(
    *,
    start_date: str,
    end_date: str,
    estado_id: str = "",
) -> dict[str, Any]:
    if not _motor_configured():
        raise FnsMotorError("FNS Motor no está configurado en .env")
    start = str(start_date or "").strip()[:10]
    end = str(end_date or "").strip()[:10]
    if not start or not end:
        raise FnsMotorError("Faltan start_date / end_date")
    params: dict[str, Any] = {
        "lang": "es",
        "hotel_id": str(settings.fns_hotel_id),
        "start_date": start,
        "end_date": end,
    }
    if str(estado_id or "").strip():
        params["estado_id"] = str(estado_id).strip()
    body = {"auth": {"channel_id": str(settings.fns_channel_id)}, "params": params}
    url = _motor_url("/get_listbooking_json.php")
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, json=body, auth=_auth())
    if response.status_code >= 400:
        raise FnsMotorError(
            f"Motor list bookings HTTP {response.status_code}",
            details=response.text[:500],
        )
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise FnsMotorError("List bookings JSON inválido", details=response.text[:500]) from exc
    rows = [_normalize_booking_row(item) for item in _as_reservation_list(payload)]
    return {"ok": True, "start_date": start, "end_date": end, "bookings": rows, "raw": payload}


def _fold_name(value: str) -> str:
    """Lowercase + strip accents so Jesús ≈ Jesus."""
    text = str(value or "").strip().lower()
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _names_match(a_first: str, a_last: str, b_first: str, b_last: str) -> bool:
    af, al = _fold_name(a_first), _fold_name(a_last)
    bf, bl = _fold_name(b_first), _fold_name(b_last)
    return bool(af and al and af == bf and al == bl)


def _booking_identity_summary(row: dict[str, Any]) -> str:
    guest = str(row.get("guest") or "").strip() or (
        f"{row.get('guest_first_name') or ''} {row.get('guest_last_name') or ''}".strip()
    )
    start = str(row.get("start_date") or "")[:10]
    end = str(row.get("end_date") or "")[:10]
    bid = str(row.get("booking_id") or "")
    return f"{guest} · {start} al {end} · {bid}".strip(" ·")


async def list_guest_active_bookings(
    *,
    username: str | None = None,
    channel: str | None = None,
    guest_first_name: str = "",
    guest_last_name: str = "",
    guest_document: str = "",
    days_ahead: int = 120,
) -> dict[str, Any]:
    """Active/upcoming bookings (document preferred; name fallback with confirmation)."""
    today = datetime.now(timezone.utc).date()
    handle = str(username or "").strip().lower().lstrip("@")
    ch = str(channel or "instagram").strip().lower() or "instagram"
    first = str(guest_first_name or "").strip()
    last = str(guest_last_name or "").strip()
    first_l = first.lower()
    last_l = last.lower()
    doc_norm = normalize_document(guest_document)

    local_rows: list[dict[str, Any]] = []
    with _BOOKINGS_LOCK:
        data = deepcopy(_load_booking_index())
    for row in data.get("bookings") or []:
        if not isinstance(row, dict):
            continue
        if not _is_active_unused(row, today=today):
            continue
        row_doc = normalize_document(
            row.get("guest_document_normalized") or row.get("guest_document")
        )
        matched = False
        if doc_norm and row_doc and row_doc == doc_norm:
            matched = True
        elif first_l and last_l and _names_match(
            first_l,
            last_l,
            str(row.get("guest_first_name") or ""),
            str(row.get("guest_last_name") or ""),
        ):
            matched = True
        elif (
            not doc_norm
            and not (first_l and last_l)
            and handle
            and str(row.get("username") or "").lower().lstrip("@") == handle
        ):
            # Weak fallback only when neither document nor name was provided.
            matched = True
        if not matched:
            continue
        if ch and str(row.get("channel") or "").lower() not in {ch, ""}:
            if str(row.get("channel") or "").strip() and not (
                (doc_norm and row_doc and row_doc == doc_norm)
                or (first_l and last_l)
            ):
                continue
        local_rows.append(row)

    # Motor scan: document first; name as fallback when no document.
    motor_rows: list[dict[str, Any]] = []
    docs_to_match: set[str] = set()
    if doc_norm:
        docs_to_match.add(doc_norm)
    for row in local_rows:
        nd = normalize_document(row.get("guest_document_normalized") or row.get("guest_document"))
        if nd:
            docs_to_match.add(nd)
    names_to_match: set[tuple[str, str]] = set()
    if first_l and last_l and not doc_norm:
        names_to_match.add((first_l, last_l))
    for row in local_rows:
        if doc_norm:
            break
        rf = str(row.get("guest_first_name") or "").strip().lower()
        rl = str(row.get("guest_last_name") or "").strip().lower()
        if rf and rl:
            names_to_match.add((rf, rl))

    if docs_to_match or names_to_match:
        start = today.isoformat()
        end = (today + timedelta(days=max(1, int(days_ahead or 120)))).isoformat()
        listed = await list_bookings(start_date=start, end_date=end)
        for row in listed.get("bookings") or []:
            if not _is_active_unused(row, today=today):
                continue
            row_doc = normalize_document(
                row.get("guest_document_normalized") or row.get("guest_document")
            )
            if docs_to_match and row_doc and row_doc in docs_to_match:
                # Passport matches also require full name when provided.
                if doc_norm and document_requires_full_name(guest_document) and first_l and last_l:
                    rf = str(row.get("guest_first_name") or "").strip().lower()
                    rl = str(row.get("guest_last_name") or "").strip().lower()
                    if not _names_match(first_l, last_l, rf, rl):
                        continue
                motor_rows.append(row)
                continue
            if names_to_match:
                rf = str(row.get("guest_first_name") or "").strip().lower()
                rl = str(row.get("guest_last_name") or "").strip().lower()
                if (rf, rl) in names_to_match:
                    motor_rows.append(row)

    by_id: dict[str, dict[str, Any]] = {}
    for row in [*motor_rows, *local_rows]:
        bid = str(row.get("booking_id") or "").strip()
        if not bid:
            continue
        by_id[bid] = row
    active = sorted(by_id.values(), key=lambda r: str(r.get("start_date") or ""))
    if doc_norm:
        matched_by = "document"
    elif first_l and last_l:
        matched_by = "name"
    elif handle:
        matched_by = "username"
    else:
        matched_by = "unknown"
    needs_identity_confirmation = matched_by in {"name", "username"} and bool(active)
    summaries = [_booking_identity_summary(row) for row in active]
    if active and needs_identity_confirmation:
        message = (
            "Encontré reserva(s) por nombre (sin documento). "
            "Muéstrale al huésped: "
            + "; ".join(summaries)
            + ". Pregunta: «¿es esa tu reserva?» "
            "Solo si dice que sí, usa cancel_room_booking o change_room_booking "
            "con guest_confirmed=true."
        )
    elif active:
        message = (
            "Este huésped ya tiene reserva(s) activa(s). "
            "Pregunta si quiere otra, cancelar o cambiar."
        )
    else:
        message = "No hay reservas activas pendientes para este huésped."
    return {
        "ok": True,
        "username": handle,
        "channel": ch,
        "guest_document_normalized": doc_norm,
        "matched_by": matched_by,
        "needs_identity_confirmation": needs_identity_confirmation,
        "booking_summaries": summaries,
        "active_count": len(active),
        "has_active_booking": bool(active),
        "bookings": active,
        "message": message,
    }


async def cancel_booking(*, booking_id: str) -> dict[str, Any]:
    """Cancel an existing Motor booking (ResStatus=Cancel)."""
    if not _motor_configured():
        raise FnsMotorError("FNS Motor no está configurado en .env")
    bid = str(booking_id or "").strip()
    if not bid:
        raise FnsMotorError("Falta booking_id")

    # Prefer details from local index; Motor list as fallback.
    local: dict[str, Any] | None = None
    with _BOOKINGS_LOCK:
        for row in _load_booking_index().get("bookings") or []:
            if isinstance(row, dict) and str(row.get("booking_id") or "") == bid:
                local = deepcopy(row)
                break

    start = str((local or {}).get("start_date") or "")[:10]
    end = str((local or {}).get("end_date") or "")[:10]
    room_code = str((local or {}).get("room_type_code") or "").strip() or "0"
    rate_code = "0"
    total = str((local or {}).get("amount_after_tax") or "0")
    cur = str((local or {}).get("currency") or "CLP")
    first = str((local or {}).get("guest_first_name") or "Guest")
    last = str((local or {}).get("guest_last_name") or "Guest")

    if not start or not end:
        # Look up near-term list for this UniqueID.
        today = datetime.now(timezone.utc).date()
        listed = await list_bookings(
            start_date=(today - timedelta(days=7)).isoformat(),
            end_date=(today + timedelta(days=180)).isoformat(),
        )
        match = next(
            (
                row
                for row in listed.get("bookings") or []
                if str(row.get("booking_id") or "") == bid
            ),
            None,
        )
        if not match:
            raise FnsMotorError("No encontré esa reserva para cancelar", details=bid)
        start = str(match.get("start_date") or "")[:10]
        end = str(match.get("end_date") or "")[:10]
        total = str(match.get("amount_after_tax") or total)
        cur = str(match.get("currency") or cur)
        first = str(match.get("guest_first_name") or first)
        last = str(match.get("guest_last_name") or last)

    stamp = _now_stamp()
    body = {
        "auth": {"channel_id": str(settings.fns_channel_id)},
        "reserva": {
            "TimeStamp": stamp,
            "ResStatus": "Cancel",
            "HotelReservations": {
                "HotelReservation": {
                    "CreateDateTime": stamp,
                    "CancellationDateTime": stamp,
                    "UniqueID": {"Type": "14", "ID": bid},
                    "RoomStays": {
                        "RoomStay": [
                            {
                                "RoomTypes": {
                                    "RoomType": {
                                        "NumberOfUnits": "1",
                                        "RoomTypeCode": room_code,
                                    }
                                },
                                "RatePlans": {"RatePlan": {"RatePlanCode": rate_code}},
                                "GuestCounts": {
                                    "GuestCount": [
                                        {"Count": "1", "AgeQualifyingCode": "10"}
                                    ]
                                },
                                "ResGuests": {
                                    "ResGuest": {
                                        "Profiles": {
                                            "ProfileInfo": [
                                                {
                                                    "Profile": {
                                                        "Customer": {
                                                            "PersonName": {
                                                                "GivenName": first,
                                                                "Surname": last,
                                                            }
                                                        }
                                                    }
                                                }
                                            ]
                                        }
                                    }
                                },
                                "Total": {
                                    "AmountAfterTax": total,
                                    "CurrencyCode": cur,
                                },
                            }
                        ]
                    },
                    "ResGlobalInfo": {
                        "TimeSpan": {"Start": start, "End": end},
                        "Total": {
                            "AmountAfterTax": total,
                            "CurrencyCode": cur,
                        },
                        "BasicPropertyInfo": {
                            "HotelCode": str(settings.fns_hotel_id),
                        },
                        "Remarks": "Cancelación solicitada por huésped Instagram/Facebook",
                    },
                }
            },
        },
    }
    url = _motor_url("/booking_json.php")
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, json=body, auth=_auth())
    text = response.text
    if response.status_code >= 400:
        raise FnsMotorError(
            f"Motor cancel HTTP {response.status_code}",
            details=text[:800],
        )
    try:
        payload = response.json() if text.strip() else {}
    except Exception:
        payload = {"raw": text[:800]}

    _mark_local_booking_cancelled(bid)
    logger.info("FNS booking cancelled external_id=%s", bid)
    return {
        "ok": True,
        "booking_id": bid,
        "status": "Cancel",
        "start_date": start,
        "end_date": end,
        "guest": f"{first} {last}".strip(),
        "motor_response": payload,
        "message": f"Reserva {bid} cancelada ({start} al {end}).",
    }
