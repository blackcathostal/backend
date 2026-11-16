from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import jwt
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.ai_sources import AiSources
from app.models.ai_generation_runs import AiGenerationRuns
from app.models.users import Users
from app.services.ai_api_sources import collect_external_api_materials
from app.services.fns_motor import (
    FnsMotorError,
    booking_allowed,
    cancel_booking,
    create_booking,
    list_guest_active_bookings,
    remember_channel_booking,
    search_availability,
)
from app.services.hostel_directions import directions_from_hostel
from app.services.instagram_knowledge import hostel_info_dict, search_common_answers
from app.services.ai_source_fetcher import SourceFetchError, fetch_source_content
from app.services.reply_context import get_reply_channel, get_reply_username
from app.services.room_rates import load_room_rates

mcp = FastMCP(
    "Black Cat Tourism Sources",
    instructions=(
        "Responde con datos del hostal y las preguntas frecuentes. "
        "Para precios y tipos de habitación usa solo preguntas frecuentes. "
        "Si el huésped está autorizado a reservar por API, usa "
        "check_guest_bookings, search_room_availability, create_room_booking, "
        "cancel_room_booking y change_room_booking. Siempre pide RUT/pasaporte. "
        "Para cambiar reserva: documento → cupo nuevas fechas → change_room_booking "
        "(cancela la vieja y crea la nueva). "
        "Si no, usa get_hostel_info y envíalos al WhatsApp. "
        "Sé vendedor: combina el dato con un beneficio real e invita a quedarse. "
        "Si falta algo, busca en la web. No inventes datos ni promociones."
    ),
    streamable_http_path="/",
)


def _booking_gate() -> dict[str, Any] | None:
    username = get_reply_username()
    channel = get_reply_channel()
    if booking_allowed(channel=channel, username=username):
        return None
    return {
        "ok": False,
        "error": (
            "Reserva por API no habilitada para este usuario. "
            "Ofrece WhatsApp con get_hostel_info."
        ),
        "channel": channel,
        "username": username,
    }


@mcp.tool(
    name="list_active_sources",
    description="Lista las fuentes HTTPS activas ordenadas por prioridad.",
    structured_output=True,
)
def list_active_sources(limit: int = 6) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit), settings.deepseek_max_sources))
    with SessionLocal() as db:
        rows = db.query(AiSources).filter(AiSources.is_active.is_(True)).all()
        recent_runs = (
            db.query(AiGenerationRuns.source_ids)
            .filter(
                AiGenerationRuns.status == "success",
                AiGenerationRuns.source_ids != "",
            )
            .order_by(AiGenerationRuns.created_at.desc(), AiGenerationRuns.id.desc())
            .limit(20)
            .all()
        )
        usage_count: Counter[int] = Counter()
        for (source_ids,) in recent_runs:
            for source_id in str(source_ids or "").split(","):
                if source_id.strip().isdigit():
                    usage_count[int(source_id)] += 1
        rows.sort(key=lambda row: (-row.priority, usage_count[row.id], row.id))
        rows = rows[:safe_limit]
        return {
            "sources": [
                {
                    "id": row.id,
                    "name": row.name,
                    "url": row.url,
                    "category": row.category,
                    "language": row.language,
                }
                for row in rows
            ]
        }


@mcp.tool(
    name="search_google_places",
    description=(
        "Busca lugares reales en Google Maps para enriquecer una guía turística de Santiago. "
        "Devuelve solo nombres, direcciones y horarios entregados por Google."
    ),
    structured_output=True,
)
async def search_google_places(query: str, max_results: int = 5) -> dict[str, Any]:
    if not settings.google_places_api_key.strip():
        return {"ok": False, "query": query, "error": "GOOGLE_PLACES_API_KEY no está configurada."}

    text_query = str(query or "").strip()[:180]
    if not text_query:
        return {"ok": False, "query": query, "error": "La consulta de Google Maps está vacía."}

    field_mask = ",".join(
        (
            "places.displayName",
            "places.formattedAddress",
            "places.googleMapsUri",
            "places.types",
            "places.rating",
            "places.regularOpeningHours",
        )
    )
    try:
        async with httpx.AsyncClient(timeout=settings.deepseek_source_timeout_seconds) as client:
            response = await client.post(
                "https://places.googleapis.com/v1/places:searchText",
                headers={
                    "X-Goog-Api-Key": settings.google_places_api_key,
                    "X-Goog-FieldMask": field_mask,
                    "Content-Type": "application/json",
                },
                json={
                    "textQuery": f"{text_query}, Santiago de Chile",
                    "languageCode": "es",
                    "regionCode": "CL",
                    "maxResultCount": max(1, min(int(max_results), 10)),
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "query": text_query, "error": f"No se pudo consultar Google Maps: {exc}"}

    places = []
    for place in payload.get("places", []):
        name = str(place.get("displayName", {}).get("text") or "").strip()
        address = str(place.get("formattedAddress") or "").strip()
        if not name or not address:
            continue
        hours = place.get("regularOpeningHours") or {}
        places.append(
            {
                "name": name,
                "address": address,
                "maps_url": place.get("googleMapsUri") or "",
                "types": place.get("types") or [],
                "rating": place.get("rating"),
                "opening_hours": hours.get("weekdayDescriptions") or [],
            }
        )
    return {"ok": True, "query": text_query, "places": places}


@mcp.tool(
    name="directions_from_hostel",
    description=(
        "Cómo llegar desde Black Cat Hostal (Compañía de Jesús 1921) a un lugar de Santiago. "
        "Prioriza metro o micro, luego auto; a pie solo si no hay metro/bus. "
        "Úsala cuando pregunten cómo llegar a La Moneda, un museo o una estación."
    ),
    structured_output=True,
)
def directions_from_hostel_tool(destination: str) -> dict[str, Any]:
    return directions_from_hostel(destination)


def _faq_payload(query: str = "") -> dict[str, Any]:
    with SessionLocal() as db:
        items = search_common_answers(db, query)
    return {
        "ok": True,
        "query": str(query or "").strip(),
        "questions": [
            {"question": item.get("question") or "", "answer": item.get("answer") or ""}
            for item in items
        ],
    }


@mcp.tool(
    name="search_frequent_questions",
    description=(
        "Lee las preguntas frecuentes de Black Cat Hostal. "
        "Úsala SIEMPRE para precios, tipos de habitación y qué incluye cada una, "
        "además de otras dudas del huésped. No uses el listado de habitaciones."
    ),
    structured_output=True,
)
def search_frequent_questions(query: str = "") -> dict[str, Any]:
    return _faq_payload(query)


@mcp.tool(
    name="search_common_answers",
    description=(
        "Lee las preguntas frecuentes. Úsala SIEMPRE para precios y tipos de habitación "
        "(no uses habitaciones del panel). También para otras dudas. "
        "Si la respuesta no está completa, busca también en la web (excepto precios)."
    ),
    structured_output=True,
)
def search_common_answers_tool(query: str = "") -> dict[str, Any]:
    return _faq_payload(query)


@mcp.tool(
    name="search_rooms",
    description=(
        "NO usar en respuestas de Instagram/Facebook para precios o tipos de habitación. "
        "Esos datos salen solo de preguntas frecuentes (search_common_answers / "
        "search_frequent_questions)."
    ),
    structured_output=True,
)
def search_rooms_tool(query: str = "") -> dict[str, Any]:
    # Soft-block: redirect the model to FAQ instead of the rooms catalog.
    return {
        "ok": False,
        "query": str(query or "").strip(),
        "rooms": [],
        "error": (
            "No uses el listado de habitaciones. Para precios y tipos de habitación "
            "llama search_common_answers o search_frequent_questions."
        ),
    }


@mcp.tool(
    name="get_hostel_info",
    description=(
        "Lee check-in, check-out, desayuno, estacionamiento, WhatsApp, correo y dirección "
        "guardados en la información del hostal. "
        "Si el huésped NO puede reservar por API, úsala para mandarlo a WhatsApp."
    ),
    structured_output=True,
)
def get_hostel_info_tool() -> dict[str, Any]:
    with SessionLocal() as db:
        info = hostel_info_dict(db)
    return {"ok": True, **info}


def _faq_guest_prices_clp() -> dict[str, Any]:
    rates = load_room_rates()
    rooms = []
    for room in rates.get("rooms") or []:
        if not isinstance(room, dict):
            continue
        rooms.append(
            {
                "slug": room.get("slug"),
                "name_es": room.get("name_es"),
                "price_from_clp": room.get("price_from_clp"),
            }
        )
    return {
        "currency": "CLP",
        "rooms": rooms,
        "note": (
            "Precios en pesos (CLP) del hostal para Instagram/Facebook. "
            "Al huésped cotiza SOLO CLP; no digas USD."
        ),
    }


def _nights_between(start_date: str, end_date: str) -> int:
    try:
        start = datetime.strptime(str(start_date)[:10], "%Y-%m-%d").date()
        end = datetime.strptime(str(end_date)[:10], "%Y-%m-%d").date()
        return max(1, (end - start).days)
    except ValueError:
        return 1


def _stay_nights(start_date: str, end_date: str) -> list[str]:
    """Nights of the stay as YYYY-MM-DD (checkout day excluded)."""
    try:
        start = datetime.strptime(str(start_date)[:10], "%Y-%m-%d").date()
        end = datetime.strptime(str(end_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        return []
    if end <= start:
        return [start.isoformat()]
    days: list[str] = []
    cursor = start
    while cursor < end:
        days.append(cursor.isoformat())
        cursor = cursor.fromordinal(cursor.toordinal() + 1)
    return days


def _clp_day_rates(start_date: str, end_date: str, price_per_night_clp: int) -> list[dict[str, str]]:
    pnight = f"{int(price_per_night_clp)}.00"
    return [{"day": day, "pnight": pnight} for day in _stay_nights(start_date, end_date)]


async def _attach_guest_pricing(
    payload: dict[str, Any], *, start_date: str, end_date: str
) -> dict[str, Any]:
    """Guest quote + FNS booking amounts in FAQ CLP."""
    faq = _faq_guest_prices_clp()
    nights = _nights_between(start_date, end_date)
    by_slug = {
        str(row.get("slug") or ""): row for row in (faq.get("rooms") or []) if isinstance(row, dict)
    }
    shared = int((by_slug.get("shared") or {}).get("price_from_clp") or 19990)
    matrimonial = int((by_slug.get("matrimonial-suite") or {}).get("price_from_clp") or 54990)
    twin = int((by_slug.get("twin") or {}).get("price_from_clp") or 54990)
    triple = int((by_slug.get("triple") or {}).get("price_from_clp") or 69990)

    rooms_out: list[dict[str, Any]] = []
    for room in payload.get("rooms") or []:
        if not isinstance(room, dict):
            continue
        name = str(room.get("room_name") or "").lower()
        if "compart" in name or "shared" in name or "bed" in name or "litera" in name:
            guest_night_clp = shared
            guest_label = "Compartida (por cama)"
        elif "triple" in name:
            guest_night_clp = triple
            guest_label = "Triple"
        elif "twin" in name or "doble" in name:
            guest_night_clp = twin
            guest_label = "Twin"
        else:
            guest_night_clp = matrimonial
            guest_label = "Matrimonial/Suite"

        guest_total_clp = guest_night_clp * nights
        booking_day_rates = _clp_day_rates(start_date, end_date, guest_night_clp)

        plans = []
        for plan in room.get("rate_plans") or []:
            if not isinstance(plan, dict):
                continue
            plans.append(
                {
                    "rate_plan_code": plan.get("rate_plan_code"),
                    "rate_name": plan.get("rate_name"),
                    "occupancy": plan.get("occupancy"),
                    # Keep Motor USD only as reference; booking uses CLP below.
                    "motor_booking_price_usd": plan.get("price"),
                    "motor_currency": plan.get("currency") or "USD",
                    "motor_day_rates_usd": plan.get("day_rates") or [],
                }
            )

        rooms_out.append(
            {
                "room_type_code": room.get("room_type_code"),
                "room_name": room.get("room_name"),
                "inventory": room.get("inventory"),
                "guest_room_label": guest_label,
                "guest_price_per_night_clp": guest_night_clp,
                "guest_total_clp": guest_total_clp,
                # Persist these into FNS booking (CurrencyCode CLP).
                "booking_currency": "CLP",
                "booking_amount_after_tax": str(guest_total_clp),
                "booking_day_rates": booking_day_rates,
                "booking_day_rates_json": json.dumps(booking_day_rates, ensure_ascii=False),
                "nights": nights,
                "rate_plans": plans,
            }
        )
    return {
        **payload,
        "rooms": rooms_out,
        "guest_prices_clp": faq,
        "quote_rule": (
            "En Instagram/Facebook cotiza SOLO pesos (guest_price_*_clp / guest_total_clp). "
            "No digas USD ni guest_price_*_usd al huésped. "
            "Al crear la reserva en FNS usa SIEMPRE CLP: "
            "currency=CLP, amount_after_tax=booking_amount_after_tax, "
            "day_rates_json=booking_day_rates_json."
        ),
    }


@mcp.tool(
    name="search_room_availability",
    description=(
        "Consulta cupo real en FNSrooms (Motor) para Black Cat Hostal. "
        "Úsala para huéspedes autorizados que quieren reservar. "
        "Devuelve room_type_code, rate_plan_code y montos CLP para create_room_booking "
        "(booking_amount_after_tax + booking_day_rates_json). "
        "Al huésped cotiza SOLO CLP (guest_price_*_clp / guest_total_clp)."
    ),
    structured_output=True,
)
async def search_room_availability_tool(
    start_date: str,
    end_date: str,
    adults: int = 2,
    children: int = 0,
    currency: str = "USD",
) -> dict[str, Any]:
    blocked = _booking_gate()
    if blocked:
        return blocked
    try:
        payload = await search_availability(
            start_date=start_date,
            end_date=end_date,
            adults=adults,
            children=children,
            currency=currency or "USD",
        )
        if not payload.get("ok"):
            return payload
        return await _attach_guest_pricing(payload, start_date=start_date, end_date=end_date)
    except FnsMotorError as exc:
        return {"ok": False, "error": str(exc), "details": getattr(exc, "details", None)}


@mcp.tool(
    name="check_guest_bookings",
    description=(
        "Revisa si este huésped ya tiene una reserva activa (aún no usada). "
        "Ideal: RUT/pasaporte (guest_document). "
        "Si NO recuerda el documento: pide nombre y apellido "
        "(guest_first_name + guest_last_name). "
        "Si matched_by=name, muéstrale booking_summaries "
        "(nombre · fechas · código) y pregunta «¿es esa tu reserva?». "
        "Solo si dice que sí → cancel/change con guest_confirmed=true. "
        "Úsala ANTES de create/change/cancel."
    ),
    structured_output=True,
)
async def check_guest_bookings_tool(
    guest_document: str = "",
    guest_first_name: str = "",
    guest_last_name: str = "",
) -> dict[str, Any]:
    blocked = _booking_gate()
    if blocked:
        return blocked
    has_doc = bool(str(guest_document or "").strip())
    has_name = bool(str(guest_first_name or "").strip() and str(guest_last_name or "").strip())
    if not has_doc and not has_name:
        return {
            "ok": False,
            "needs_identity": True,
            "error": "missing_identity",
            "message": (
                "Pide RUT/pasaporte. Si no lo recuerda, pide nombre y apellido "
                "para buscar la reserva y verificarla con él."
            ),
        }
    try:
        return await list_guest_active_bookings(
            username=get_reply_username(),
            channel=get_reply_channel(),
            guest_first_name=guest_first_name,
            guest_last_name=guest_last_name,
            guest_document=guest_document,
        )
    except FnsMotorError as exc:
        return {"ok": False, "error": str(exc), "details": getattr(exc, "details", None)}


@mcp.tool(
    name="cancel_room_booking",
    description=(
        "Cancela una reserva FNSrooms del huésped (ResStatus=Cancel). "
        "Usa booking_id de check_guest_bookings. "
        "Si la encontró solo por nombre (sin RUT), exige que el huésped "
        "haya confirmado «sí, esa es mi reserva» y pasa guest_confirmed=true "
        "+ nombre/apellido."
    ),
    structured_output=True,
)
async def cancel_room_booking_tool(
    booking_id: str,
    guest_confirmed: bool = False,
    guest_document: str = "",
    guest_first_name: str = "",
    guest_last_name: str = "",
) -> dict[str, Any]:
    blocked = _booking_gate()
    if blocked:
        return blocked
    has_doc = bool(str(guest_document or "").strip())
    if not has_doc and not guest_confirmed:
        return {
            "ok": False,
            "needs_confirmation": True,
            "error": "identity_not_confirmed",
            "message": (
                "Sin documento: primero muéstrale la reserva encontrada "
                "(nombre y fechas) y espera que diga que sí. "
                "Luego llama cancel_room_booking con guest_confirmed=true."
            ),
        }
    try:
        return await cancel_booking(booking_id=booking_id)
    except FnsMotorError as exc:
        return {"ok": False, "error": str(exc), "details": getattr(exc, "details", None)}


def _parse_day_rates_json(
    day_rates_json: str,
    *,
    start_date: str,
    end_date: str,
    amount_after_tax: str,
) -> tuple[list[dict[str, str]] | None, dict[str, Any] | None]:
    day_rates: list[dict[str, str]] = []
    raw = str(day_rates_json or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                day_rates = [row for row in parsed if isinstance(row, dict)]
        except json.JSONDecodeError:
            return None, {
                "ok": False,
                "error": "day_rates_json inválido; envía un JSON array.",
            }
    if not day_rates:
        nights = _nights_between(start_date, end_date)
        try:
            total = int(float(str(amount_after_tax).replace(",", ".")))
        except ValueError:
            total = 0
        per_night = int(total / nights) if nights else total
        day_rates = _clp_day_rates(start_date, end_date, per_night)
    return day_rates, None


async def _create_and_remember_booking(
    *,
    start_date: str,
    end_date: str,
    room_type_code: str,
    rate_plan_code: str,
    amount_after_tax: str,
    guest_first_name: str,
    guest_last_name: str,
    guest_document: str,
    guest_document_type: str = "",
    adults: int = 2,
    children: int = 0,
    units: int = 1,
    guest_email: str = "",
    guest_phone: str = "",
    remarks: str = "",
    day_rates_json: str = "[]",
) -> dict[str, Any]:
    day_rates, err = _parse_day_rates_json(
        day_rates_json,
        start_date=start_date,
        end_date=end_date,
        amount_after_tax=amount_after_tax,
    )
    if err:
        return err
    created = await create_booking(
        start_date=start_date,
        end_date=end_date,
        room_type_code=room_type_code,
        rate_plan_code=rate_plan_code,
        amount_after_tax=amount_after_tax,
        currency="CLP",
        adults=adults,
        children=children,
        units=units,
        guest_first_name=guest_first_name,
        guest_last_name=guest_last_name,
        guest_document=guest_document,
        guest_document_type=guest_document_type,
        guest_email=guest_email,
        guest_phone=guest_phone,
        remarks=remarks or "Reserva Instagram/Facebook Black Cat (CLP)",
        day_rates=day_rates or [],
    )
    remember_channel_booking(
        booking=created,
        username=get_reply_username(),
        channel=get_reply_channel(),
    )
    return created


@mcp.tool(
    name="create_room_booking",
    description=(
        "Crea una reserva real en FNSrooms Motor. Solo para huéspedes autorizados. "
        "Antes: pide RUT/pasaporte, check_guest_bookings y search_room_availability. "
        "guest_document es obligatorio (RUT o pasaporte). "
        "Si ya tiene reserva activa y el huésped no confirmó otra, NO crees: "
        "avisa fechas y pregunta si quiere otra, cancelar, o CAMBIAR "
        "(change_room_booking). "
        "Si insiste en otra reserva adicional, confirm_extra_booking=true. "
        "Guarda en CLP. Confirma solo total CLP."
    ),
    structured_output=True,
)
async def create_room_booking_tool(
    start_date: str,
    end_date: str,
    room_type_code: str,
    rate_plan_code: str,
    amount_after_tax: str,
    guest_first_name: str,
    guest_last_name: str,
    guest_document: str,
    currency: str = "CLP",
    guest_document_type: str = "",
    adults: int = 2,
    children: int = 0,
    units: int = 1,
    guest_email: str = "",
    guest_phone: str = "",
    remarks: str = "",
    day_rates_json: str = "[]",
    confirm_extra_booking: bool = False,
) -> dict[str, Any]:
    blocked = _booking_gate()
    if blocked:
        return blocked
    has_doc = bool(str(guest_document or "").strip())
    has_name = bool(str(guest_first_name or "").strip() and str(guest_last_name or "").strip())
    if not has_doc and not has_name:
        return {
            "ok": False,
            "needs_identity": True,
            "error": "missing_identity",
            "message": (
                "Pide RUT/pasaporte. Si no lo recuerda, nombre y apellido "
                "(y confirma la reserva encontrada antes de seguir)."
            ),
        }
    # Soft gate: warn if this guest already has an unused booking.
    if not confirm_extra_booking:
        try:
            existing = await list_guest_active_bookings(
                username=get_reply_username(),
                channel=get_reply_channel(),
                guest_first_name=guest_first_name,
                guest_last_name=guest_last_name,
                guest_document=guest_document,
            )
        except FnsMotorError:
            existing = {"has_active_booking": False, "bookings": []}
        if existing.get("has_active_booking"):
            first = (existing.get("bookings") or [{}])[0]
            return {
                "ok": False,
                "needs_confirmation": True,
                "error": "already_has_active_booking",
                "message": (
                    "Este huésped ya tiene una reserva activa. "
                    "Dile las fechas y pregunta: ¿otra, cancelar esa, o cambiarla? "
                    "Para cambiar fechas usa change_room_booking "
                    "(cancela la vieja y crea la nueva). "
                    "Para solo cancelar: cancel_room_booking. "
                    "Si insiste en otra adicional: create_room_booking "
                    "con confirm_extra_booking=true."
                ),
                "active_bookings": existing.get("bookings") or [],
                "example_booking_id": first.get("booking_id"),
                "example_dates": {
                    "start_date": first.get("start_date"),
                    "end_date": first.get("end_date"),
                },
            }
    try:
        return await _create_and_remember_booking(
            start_date=start_date,
            end_date=end_date,
            room_type_code=room_type_code,
            rate_plan_code=rate_plan_code,
            amount_after_tax=amount_after_tax,
            guest_first_name=guest_first_name,
            guest_last_name=guest_last_name,
            guest_document=guest_document,
            guest_document_type=guest_document_type,
            adults=adults,
            children=children,
            units=units,
            guest_email=guest_email,
            guest_phone=guest_phone,
            remarks=remarks,
            day_rates_json=day_rates_json,
        )
    except FnsMotorError as exc:
        return {"ok": False, "error": str(exc), "details": getattr(exc, "details", None)}


@mcp.tool(
    name="change_room_booking",
    description=(
        "Cambia una reserva: busca la vieja (RUT/pasaporte o nombre+apellido), "
        "verifica cupo en fechas NUEVAS, cancela la anterior y crea la nueva. "
        "Si no hay documento y se buscó por nombre, el huésped debió confirmar "
        "«sí, esa es mi reserva» → guest_confirmed=true. "
        "Antes: search_room_availability y pasar códigos/montos CLP."
    ),
    structured_output=True,
)
async def change_room_booking_tool(
    start_date: str,
    end_date: str,
    room_type_code: str,
    rate_plan_code: str,
    amount_after_tax: str,
    guest_first_name: str,
    guest_last_name: str,
    guest_document: str = "",
    old_booking_id: str = "",
    guest_document_type: str = "",
    guest_confirmed: bool = False,
    adults: int = 2,
    children: int = 0,
    units: int = 1,
    guest_email: str = "",
    guest_phone: str = "",
    remarks: str = "",
    day_rates_json: str = "[]",
) -> dict[str, Any]:
    blocked = _booking_gate()
    if blocked:
        return blocked
    document = str(guest_document or "").strip()
    has_name = bool(str(guest_first_name or "").strip() and str(guest_last_name or "").strip())
    if not document and not has_name:
        return {
            "ok": False,
            "needs_identity": True,
            "error": "missing_identity",
            "message": (
                "Pide RUT/pasaporte. Si no lo recuerda, nombre y apellido, "
                "verifica la reserva con él y si confirma usa guest_confirmed=true."
            ),
        }
    if not document and not guest_confirmed:
        return {
            "ok": False,
            "needs_confirmation": True,
            "error": "identity_not_confirmed",
            "message": (
                "Sin documento: muéstrale la reserva (nombre y fechas) y espera "
                "que diga que sí. Luego change_room_booking con guest_confirmed=true."
            ),
        }

    try:
        existing = await list_guest_active_bookings(
            username=get_reply_username(),
            channel=get_reply_channel(),
            guest_first_name=guest_first_name,
            guest_last_name=guest_last_name,
            guest_document=document,
        )
    except FnsMotorError as exc:
        return {"ok": False, "error": str(exc), "details": getattr(exc, "details", None)}

    active = [row for row in (existing.get("bookings") or []) if isinstance(row, dict)]
    if not active:
        return {
            "ok": False,
            "error": "no_active_booking",
            "message": (
                "No encontré reserva activa con esos datos. "
                "Pide RUT/pasaporte o nombre y apellido de nuevo."
            ),
        }

    wanted_old = str(old_booking_id or "").strip()
    old_row = None
    if wanted_old:
        old_row = next(
            (row for row in active if str(row.get("booking_id") or "") == wanted_old),
            None,
        )
        if not old_row:
            return {
                "ok": False,
                "error": "old_booking_not_found",
                "message": "Ese booking_id no coincide con una reserva activa de este documento.",
                "active_bookings": active,
            }
    else:
        if len(active) > 1:
            return {
                "ok": False,
                "needs_booking_id": True,
                "error": "multiple_active_bookings",
                "message": (
                    "Hay varias reservas activas. Pregunta cuál cambiar "
                    "y pasa old_booking_id."
                ),
                "active_bookings": active,
            }
        old_row = active[0]

    old_id = str(old_row.get("booking_id") or "").strip()
    if not old_id:
        return {"ok": False, "error": "missing_old_booking_id"}

    # Verify new-date availability BEFORE cancelling the old booking.
    try:
        avail = await search_availability(
            start_date=start_date,
            end_date=end_date,
            adults=adults,
            children=children,
            currency="USD",
        )
        priced = await _attach_guest_pricing(
            avail, start_date=start_date, end_date=end_date
        )
    except FnsMotorError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "details": getattr(exc, "details", None),
            "message": "No pude verificar disponibilidad de las fechas nuevas; no cancelé la reserva vieja.",
        }

    room_code = str(room_type_code or "").strip()
    match = next(
        (
            room
            for room in (priced.get("rooms") or [])
            if isinstance(room, dict)
            and str(room.get("room_type_code") or "") == room_code
        ),
        None,
    )
    if not match:
        return {
            "ok": False,
            "error": "no_availability_new_dates",
            "message": (
                "No hay cupo para esas fechas/habitación. "
                "No cancelé la reserva anterior. Ofrece otras fechas."
            ),
            "old_booking": old_row,
            "availability": priced,
        }

    try:
        cancelled = await cancel_booking(booking_id=old_id)
    except FnsMotorError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "details": getattr(exc, "details", None),
            "message": "No pude cancelar la reserva anterior; no creé la nueva.",
            "old_booking": old_row,
        }

    create_document = document or str(old_row.get("guest_document") or "").strip()
    create_doc_type = guest_document_type or str(old_row.get("guest_document_type") or "")
    try:
        created = await _create_and_remember_booking(
            start_date=start_date,
            end_date=end_date,
            room_type_code=room_type_code,
            rate_plan_code=rate_plan_code,
            amount_after_tax=amount_after_tax,
            guest_first_name=guest_first_name,
            guest_last_name=guest_last_name,
            guest_document=create_document,
            guest_document_type=create_doc_type,
            adults=adults,
            children=children,
            units=units,
            guest_email=guest_email,
            guest_phone=guest_phone,
            remarks=remarks
            or f"Cambio de reserva (reemplaza {old_id}) Instagram/Facebook Black Cat",
            day_rates_json=day_rates_json,
        )
    except FnsMotorError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "details": getattr(exc, "details", None),
            "cancelled_old": True,
            "cancelled_booking_id": old_id,
            "message": (
                f"Cancelé la reserva anterior ({old_id}) pero falló crear la nueva. "
                "Pide disculpas y ofrece reintentar create_room_booking "
                "con confirm_extra_booking=true."
            ),
        }

    return {
        "ok": True,
        "changed": True,
        "cancelled_booking": cancelled,
        "new_booking": created,
        "message": (
            f"Reserva cambiada: cancelé {old_id} "
            f"({old_row.get('start_date')} al {old_row.get('end_date')}) "
            f"y creé {created.get('booking_id')} "
            f"({created.get('start_date')} al {created.get('end_date')})."
        ),
    }


@mcp.tool(
    name="get_directions_from_hostel",
    description=(
        "Cómo llegar desde Black Cat Hostal (Compañía de Jesús 1921) a un lugar de Santiago. "
        "Prioriza metro o micro, luego auto; a pie solo si no hay metro/bus."
    ),
    structured_output=True,
)
def get_directions_from_hostel_tool(destination: str) -> dict[str, Any]:
    return directions_from_hostel(destination)


def _web_result_url(raw_href: str) -> str:
    href = unquote(str(raw_href or "").strip())
    if href.startswith("//"):
        href = f"https:{href}"
    parsed = urlparse(href)
    if "duckduckgo.com" in (parsed.hostname or "") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        href = unquote(target)
    if href.startswith("https://"):
        return href
    return ""


def _strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", text).strip()


def _parse_duckduckgo(html: str, limit: int) -> list[dict[str, str]]:
    blocks = re.findall(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>(?:.*?)class="result__snippet"[^>]*>(.*?)</(?:a|td|span)>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for href, title_html, snippet_html in blocks:
        url = _web_result_url(href)
        title = _strip_tags(title_html)
        snippet = _strip_tags(snippet_html)
        if not url or not title or url in seen:
            continue
        seen.add(url)
        results.append({"title": title[:180], "url": url, "snippet": snippet[:320]})
        if len(results) >= limit:
            break
    return results


async def _wikipedia_search(query: str, limit: int) -> list[dict[str, str]]:
    try:
        async with httpx.AsyncClient(timeout=settings.deepseek_source_timeout_seconds) as client:
            response = await client.get(
                "https://es.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": limit,
                    "format": "json",
                    "utf8": 1,
                },
                headers={"User-Agent": "BlackCatTourismBot/1.0 (+https://blackcathostal.com)"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    results: list[dict[str, str]] = []
    for item in (payload.get("query") or {}).get("search") or []:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        slug = title.replace(" ", "_")
        results.append(
            {
                "title": title,
                "url": f"https://es.wikipedia.org/wiki/{slug}",
                "snippet": _strip_tags(str(item.get("snippet") or ""))[:320],
            }
        )
    return results


@mcp.tool(
    name="search_web",
    description=(
        "Busca en la web cuando la respuesta no está en las preguntas frecuentes "
        "ni en la información del hostal. No la uses para precios de habitación. "
        "Sirve para lugares de Santiago, horarios públicos y datos que no están en el panel."
    ),
    structured_output=True,
)
async def search_web(query: str, max_results: int = 5) -> dict[str, Any]:
    text = str(query or "").strip()[:180]
    if not text:
        return {"ok": False, "query": text, "error": "La búsqueda está vacía."}
    limit = max(1, min(int(max_results or 5), 5))
    results: list[dict[str, str]] = []
    error = ""
    try:
        async with httpx.AsyncClient(
            timeout=settings.deepseek_source_timeout_seconds,
            follow_redirects=True,
        ) as client:
            response = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": text},
                headers={
                    "User-Agent": "BlackCatTourismBot/1.0 (+https://blackcathostal.com)",
                    "Accept": "text/html",
                },
            )
            response.raise_for_status()
            results = _parse_duckduckgo(response.text, limit)
    except (httpx.HTTPError, ValueError) as exc:
        error = str(exc)
    if not results:
        results = await _wikipedia_search(text, limit)
    if not results:
        return {"ok": False, "query": text, "results": [], "error": error or "Sin resultados web."}
    return {"ok": True, "query": text, "results": results}


@mcp.tool(
    name="read_web_page",
    description=(
        "Lee el texto de una página HTTPS devuelta por search_web. "
        "No uses direcciones que no hayan salido de una tool."
    ),
    structured_output=True,
)
async def read_web_page(url: str) -> dict[str, Any]:
    try:
        material = await fetch_source_content(
            str(url or "").strip(),
            timeout_seconds=settings.deepseek_source_timeout_seconds,
            max_bytes=settings.deepseek_source_max_bytes,
        )
    except (SourceFetchError, Exception) as exc:
        return {"ok": False, "url": url, "error": str(exc)}
    return {
        "ok": True,
        "url": material.get("url") or url,
        "title": material.get("title") or "",
        "text": str(material.get("text") or "")[:1800],
    }


_SKIPPED_MEDIA_HOSTS = (
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "twitter.com",
    "x.com",
)


def _direct_file(url: str, extensions: tuple[str, ...]) -> bool:
    path = urlparse(str(url or "")).path.lower()
    return path.endswith(extensions)


def _publishable_host(url: str) -> bool:
    host = (urlparse(str(url or "")).hostname or "").lower()
    return bool(host) and not any(host == name or host.endswith(f".{name}") for name in _SKIPPED_MEDIA_HOSTS)


def _usable_image(url: str) -> bool:
    if not _publishable_host(url):
        return False
    path = urlparse(url).path.lower()
    return not path.endswith((".svg", ".gif", ".ico", ".avif", ".bmp"))


def _rejected_photo(url: str, alt: str = "") -> bool:
    host = (urlparse(url).hostname or "").lower()
    if "wikipedia.org" in host or "wikimedia.org" in host:
        return True
    blob = f"{url} {alt}".lower()
    return any(
        word in blob
        for word in (
            "logo",
            "icon",
            "icono",
            "avatar",
            "sprite",
            "placeholder",
            "gravatar",
            "1x1",
            "pixel",
            "escudo",
            "bandera",
            "flag",
            "mapa",
            "/map.",
            "grabado",
            "engraving",
            "lithograph",
            "author",
            "autor",
        )
    )


@mcp.tool(
    name="find_web_media",
    description=(
        "Lee una página HTTPS ya encontrada con search_web y devuelve fotos y videos "
        "que se pueden publicar, junto con el título y la URL de la fuente. "
        "Úsala para crear un post. No inventes otras URLs."
    ),
    structured_output=True,
)
async def find_web_media(url: str) -> dict[str, Any]:
    try:
        material = await fetch_source_content(
            str(url or "").strip(),
            timeout_seconds=settings.deepseek_source_timeout_seconds,
            max_bytes=settings.deepseek_source_max_bytes,
        )
    except (SourceFetchError, Exception) as exc:
        return {"ok": False, "url": url, "error": str(exc)}
    page_url = str(material.get("url") or url)
    candidates = [
        item
        for item in (material.get("image_candidates") or [])
        if isinstance(item, dict)
        and _usable_image(str(item.get("url") or ""))
        and not _rejected_photo(str(item.get("url") or ""), str(item.get("alt") or ""))
    ]
    candidates.sort(key=lambda item: 0 if str(item.get("is_meta")) == "True" else 1)
    if not candidates:
        candidates = [
            {"url": item, "alt": "", "is_meta": "False"}
            for item in (material.get("image_urls") or [])
            if _usable_image(item) and not _rejected_photo(item)
        ]
    images = [
        {
            "url": str(item.get("url") or ""),
            "alt": str(item.get("alt") or ""),
            "is_meta": str(item.get("is_meta") or ""),
        }
        for item in candidates[:6]
        if item.get("url")
    ]
    videos = [
        {"url": item}
        for item in (material.get("video_urls") or [])
        if _publishable_host(item) and _direct_file(item, (".mp4", ".mov"))
    ][:2]
    title = str(material.get("title") or "").strip()
    host = urlparse(page_url).hostname or ""
    return {
        "ok": True,
        "url": page_url,
        "title": title,
        "source_name": title or host,
        "text": str(material.get("text") or "")[:700],
        "images": images,
        "videos": videos,
    }


_BORING_PHOTO = (
    "logo",
    "mapa",
    "map",
    "bandera",
    "flag",
    "escudo",
    "icon",
    "icono",
    "diagrama",
    "diagram",
    "coat of arms",
)


@mcp.tool(
    name="search_tourism_photo",
    description=(
        "Busca una foto atractiva de turismo en Santiago o de un lugar concreto: "
        "calles, cerros, plazas, cafés, barrios. No devuelve mapas, logos ni banderas."
    ),
    structured_output=True,
)
async def search_tourism_photo(query: str) -> dict[str, Any]:
    text = str(query or "").strip()[:140]
    if not text:
        return {"ok": False, "query": text, "photos": [], "error": "La búsqueda está vacía."}
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"File:{text} Santiago",
        "gsrnamespace": "6",
        "gsrlimit": "12",
        "prop": "imageinfo",
        "iiprop": "url|mime",
        "iiurlwidth": "1400",
        "format": "json",
        "formatversion": "2",
    }
    try:
        async with httpx.AsyncClient(timeout=settings.deepseek_source_timeout_seconds) as client:
            response = await client.get(
                "https://commons.wikimedia.org/w/api.php",
                params=params,
                headers={"User-Agent": "BlackCatTourismBot/1.0 (+https://blackcathostal.com)"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "query": text, "photos": [], "error": str(exc)}
    photos: list[dict[str, str]] = []
    for page in (payload.get("query") or {}).get("pages") or []:
        if not isinstance(page, dict):
            continue
        title = str(page.get("title") or "").replace("File:", "").strip()
        if any(word in title.lower() for word in _BORING_PHOTO):
            continue
        info = (page.get("imageinfo") or [{}])[0]
        mime = str(info.get("mime") or "").lower()
        image_url = str(info.get("thumburl") or info.get("url") or "")
        source_url = str(info.get("descriptionurl") or "")
        if mime not in {"image/jpeg", "image/png", "image/webp"} or not image_url.startswith("https://"):
            continue
        photos.append(
            {
                "title": title[:180],
                "image_url": image_url,
                "source_url": source_url or "https://commons.wikimedia.org",
            }
        )
        if len(photos) >= 4:
            break
    if not photos:
        return {"ok": False, "query": text, "photos": [], "error": "No hay una foto atractiva para ese tema."}
    return {"ok": True, "query": text, "photos": photos}


def tool_result_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    return {}


async def call_reply_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one MCP tool for a DeepSeek Instagram or Facebook reply."""
    tool_name = str(name or "").strip()
    if tool_name == "search_rooms":
        # Prices / room types must come only from FAQ.
        return await call_reply_tool("search_common_answers", arguments or {})
    try:
        result = await mcp.call_tool(tool_name, arguments or {})
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    payload = tool_result_payload(result)
    if payload:
        return payload
    return {"ok": False, "error": f"La tool {tool_name} no devolvió datos."}


async def collect_google_places(queries: list[str]) -> list[dict[str, Any]]:
    """Run the Google Maps MCP tool for unique article-related queries."""
    def payload_from_result(result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result
        if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
            return result[1]
        structured = getattr(result, "structured_content", None)
        return structured if isinstance(structured, dict) else {}

    places: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in queries[:4]:
        normalized = str(query or "").strip()
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        result = await mcp.call_tool("search_google_places", {"query": normalized, "max_results": 5})
        payload = payload_from_result(result)
        if payload.get("ok"):
            places.extend(payload.get("places", []))
    unique_places: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for place in places:
        key = str(place.get("name") or "").strip().lower()
        if key and key not in seen_names:
            seen_names.add(key)
            unique_places.append(place)
    return unique_places


@mcp.tool(
    name="read_source",
    description="Lee el texto útil de una fuente activa configurada.",
    structured_output=True,
)
async def read_source(source_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        source = (
            db.query(AiSources)
            .filter(AiSources.id == int(source_id), AiSources.is_active.is_(True))
            .first()
        )
        if not source:
            return {"ok": False, "source_id": source_id, "error": "Fuente no encontrada o inactiva."}
        url = source.url
        source_name = source.name

    try:
        material = await fetch_source_content(
            url,
            timeout_seconds=settings.deepseek_source_timeout_seconds,
            max_bytes=settings.deepseek_source_max_bytes,
        )
    except Exception as exc:
        with SessionLocal() as db:
            failed = db.query(AiSources).filter(AiSources.id == int(source_id)).first()
            if failed:
                failed.last_checked_at = datetime.now(timezone.utc)
                failed.last_status = "error"
                failed.last_error = str(exc)[:1000]
                db.commit()
        return {"ok": False, "source_id": source_id, "name": source_name, "error": str(exc)}

    with SessionLocal() as db:
        checked = db.query(AiSources).filter(AiSources.id == int(source_id)).first()
        if checked:
            checked.last_checked_at = datetime.now(timezone.utc)
            checked.last_status = "ok"
            checked.last_error = None
            db.commit()

    return {
        "ok": True,
        "source_id": source_id,
        "name": source_name,
        "url": material["url"],
        "title": material["title"],
        "text": material["text"][: settings.deepseek_source_char_limit],
        "image_url": material.get("image_url", ""),
        "image_urls": material.get("image_urls", []),
        "image_candidates": material.get("image_candidates", []),
    }


async def collect_source_material() -> tuple[list[dict[str, Any]], list[int]]:
    def payload_from_result(result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result
        if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
            return result[1]
        structured = getattr(result, "structured_content", None)
        return structured if isinstance(structured, dict) else {}

    listed = await mcp.call_tool(
        "list_active_sources",
        {"limit": settings.deepseek_max_sources},
    )
    source_payload = payload_from_result(listed)
    materials: list[dict[str, Any]] = []
    source_ids: list[int] = []
    for source in source_payload.get("sources", []):
        result = await mcp.call_tool("read_source", {"source_id": int(source["id"])})
        result_payload = payload_from_result(result)
        if result_payload.get("ok"):
            materials.append(result_payload)
            source_ids.append(int(source["id"]))
    materials.extend(await collect_external_api_materials())
    return materials, source_ids


def protected_mcp_app():
    mcp_app = mcp.streamable_http_app()

    async def application(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            authorization = headers.get(b"authorization", b"").decode("latin-1")
            token = authorization.removeprefix("Bearer ").strip()
            authenticated = False
            if token:
                try:
                    payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
                    email = payload.get("sub")
                    with SessionLocal() as db:
                        user = (
                            db.query(Users)
                            .filter(Users.email == email, Users.is_active.is_(True))
                            .first()
                        )
                        authenticated = bool(user)
                except (jwt.PyJWTError, TypeError, ValueError):
                    authenticated = False
            if not authenticated:
                await JSONResponse(
                    {"detail": "Unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return
        await mcp_app(scope, receive, send)

    return application
