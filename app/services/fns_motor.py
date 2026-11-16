"""FNSrooms Motor API: availability + booking (Basic Auth)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


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
                "inventory": str(room.get("invcount") or ""),
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
    }


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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
        }
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
    return {
        "ok": True,
        "booking_id": booking_id,
        "hotel_id": str(settings.fns_hotel_id),
        "start_date": start,
        "end_date": end,
        "room_type_code": room_code,
        "rate_plan_code": rate_code,
        "amount_after_tax": total,
        "currency": cur,
        "guest": f"{first} {last}".strip(),
        "motor_response": payload,
    }
