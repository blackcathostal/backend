"""Knowledge lookup tools for DeepSeek Instagram replies (rooms, facts, FAQ)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.hostel_info import CommonAnswers, HostelInfo
from app.services.hostel_directions import directions_from_hostel, driving_eta_from_hostel
from app.models.rooms import Rooms


def get_or_create_hostel_info(db: Session) -> HostelInfo:
    row = db.query(HostelInfo).filter(HostelInfo.id == 1).first()
    if row:
        return row
    row = HostelInfo(id=1)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def hostel_info_dict(db: Session) -> dict[str, Any]:
    row = get_or_create_hostel_info(db)
    return {
        "check_in_time": row.check_in_time,
        "check_out_time": row.check_out_time,
        "breakfast_hours": row.breakfast_hours,
        "has_parking": bool(row.has_parking),
        "parking_notes": row.parking_notes or "",
        "whatsapp_url": row.whatsapp_url,
        "address": row.address,
        "email": row.email,
        "website": row.website,
        "extra_notes": row.extra_notes or "",
    }


def update_hostel_info(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    row = get_or_create_hostel_info(db)
    for key in (
        "check_in_time",
        "check_out_time",
        "breakfast_hours",
        "parking_notes",
        "whatsapp_url",
        "address",
        "email",
        "website",
        "extra_notes",
    ):
        if key in payload and payload[key] is not None:
            setattr(row, key, str(payload[key]).strip())
    if "has_parking" in payload and payload["has_parking"] is not None:
        row.has_parking = bool(payload["has_parking"])
    db.commit()
    db.refresh(row)
    return hostel_info_dict(db)


def search_rooms(db: Session, query: str | None = None) -> list[dict[str, Any]]:
    rooms = db.query(Rooms).order_by(Rooms.type.asc(), Rooms.price.asc()).all()
    needle = (query or "").strip().lower()
    out: list[dict[str, Any]] = []
    for room in rooms:
        item = {
            "id": room.id,
            "name": room.name,
            "type": room.type,
            "capacity": room.capacity,
            "price_clp": int(room.price or 0),
            "price_label": f"${int(room.price or 0):,}".replace(",", "."),
            "features": getattr(room, "features", None) or "",
        }
        if needle:
            blob = " ".join(
                [
                    str(item["name"]),
                    str(item["type"]),
                    str(item["features"]),
                ]
            ).lower()
            if needle not in blob:
                continue
        out.append(item)
    return out


_INSTRUCTION_MARKERS = (
    "comparte el whatsapp",
    "comparte la dirección",
    "share whatsapp",
    "share address",
    "usa el horario",
    "usa si hay estacionamiento",
    "use get_hostel_info",
    "use check_out_time",
    "use has_parking",
    "consulta las habitaciones",
    "call search_rooms",
    "el horario es el que está guardado",
    "hours from breakfast",
    "no confirmes cupos",
    "do not confirm exact",
    "depende de las condiciones de la reserva. pide que escriban",
    "depends on booking",
    "pide fechas y cantidad de personas por whatsapp para cotizar",
    "ask for dates and headcount",
    "invite whatsapp",
    "offer whatsapp",
)


def _is_instruction(text: str) -> bool:
    sample = (text or "").strip().lower()
    return any(marker in sample for marker in _INSTRUCTION_MARKERS)


def _is_price_faq(topic: str, question: str) -> bool:
    if (topic or "").strip().lower() == "prices":
        return True
    sample = (question or "").strip().lower()
    return "cuesta una habitación" in sample or "cuesta una habitacion" in sample


def _whatsapp_phone(whatsapp: str) -> str:
    from app.services.deepseek_instagram import whatsapp_phone_display

    return whatsapp_phone_display(whatsapp)


def _prices_from_rooms(rooms: list[dict[str, Any]], whatsapp: str) -> str:
    del rooms, whatsapp  # Keep signature; prefer FAQ text, not a room-by-room dump.
    return (
        "Tarifas referenciales por noche (pueden variar): "
        "Matrimonial/Suite $54.990; Twin $54.990; Triple $69.990; cama $19.990. "
        "Mensual $1.500.000."
    )


def _live_faq_answer(
    topic: str,
    question: str,
    guide: str,
    info: dict[str, Any],
    rooms: list[dict[str, Any]],
) -> str:
    """Turn an internal instruction into the real hostel values."""
    stored = (guide or "").strip()
    whatsapp = str(info.get("whatsapp_url") or "").strip()
    phone = _whatsapp_phone(whatsapp)
    if _is_price_faq(topic, question):
        return _prices_from_rooms(rooms, whatsapp)
    if stored and not _is_instruction(stored):
        from app.services.deepseek_instagram import sanitize_outbound_message

        return sanitize_outbound_message(stored)
    email = str(info.get("email") or "").strip()
    address = str(info.get("address") or "").strip()
    check_in = str(info.get("check_in_time") or "").strip()
    check_out = str(info.get("check_out_time") or "").strip()
    breakfast = str(info.get("breakfast_hours") or "").strip()
    parking_notes = str(info.get("parking_notes") or "").strip()
    key = (topic or "").strip().lower()
    if key == "whatsapp":
        return f"WhatsApp {phone} o correo {email}."
    if key == "checkin":
        return f"Check-in a las {check_in}."
    if key == "checkout":
        return f"Check-out a las {check_out}."
    if key == "breakfast":
        hours = breakfast.replace("-", " a ", 1)
        return f"Desayuno incluido, de {hours}."
    if key == "parking":
        if info.get("has_parking"):
            return f"Sí, hay estacionamiento. {parking_notes}".strip()
        return parking_notes or "No hay estacionamiento en el hostal."
    if key == "location":
        return f"Estamos en {address}."
    if key == "groups":
        return "Sí recibimos grupos. Indica fechas y personas para cotizar."
    if key == "cancellation":
        return f"Depende de tu reserva. Escríbenos a {email} con el código."
    if key == "availability":
        return "Indica las fechas y te orientamos."
    if key == "mensual":
        return "La tarifa mensual de habitación es $1.500.000 CLP."
    return stored


def _faq_payload(row: CommonAnswers, info: dict[str, Any], rooms: list[dict[str, Any]]) -> dict[str, Any]:
    answer = _live_faq_answer(row.topic, row.question, row.answer_guide, info, rooms)
    return {
        "id": row.id,
        "topic": row.topic,
        "keywords": row.keywords,
        "question": row.question,
        "answer": answer,
        "answer_guide": answer,
        "sort_order": row.sort_order,
        "is_active": row.is_active,
    }


def search_common_answers(db: Session, query: str | None = None) -> list[dict[str, Any]]:
    rows = (
        db.query(CommonAnswers)
        .filter(CommonAnswers.is_active.is_(True))
        .order_by(CommonAnswers.sort_order.asc(), CommonAnswers.id.asc())
        .all()
    )
    info = hostel_info_dict(db)
    rooms = search_rooms(db)
    needle = (query or "").strip().lower()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = _faq_payload(row, info, rooms)
        if needle:
            blob = f"{row.topic} {row.keywords} {row.question} {row.answer_guide}".lower()
            tokens = [t for t in needle.replace("?", " ").split() if t]
            keys = [k.strip().lower() for k in (row.keywords or "").split(",") if k.strip()]
            if needle not in blob and not any(tok in blob for tok in tokens) and not any(
                k in needle for k in keys
            ):
                continue
        out.append(item)
    if needle and not out:
        return [_faq_payload(row, info, rooms) for row in rows]
    return out


def list_common_answers_admin(db: Session) -> list[dict[str, Any]]:
    rows = db.query(CommonAnswers).order_by(CommonAnswers.sort_order.asc(), CommonAnswers.id.asc()).all()
    info = hostel_info_dict(db)
    rooms = search_rooms(db)
    return [_faq_payload(row, info, rooms) for row in rows]


def upsert_common_answer(db: Session, payload: dict[str, Any], answer_id: int | None = None) -> dict[str, Any]:
    if answer_id:
        row = db.query(CommonAnswers).filter(CommonAnswers.id == answer_id).first()
        if not row:
            raise ValueError("Common answer not found")
    else:
        row = CommonAnswers()
        db.add(row)
    row.topic = str(payload.get("topic") or "general").strip()[:80]
    row.keywords = str(payload.get("keywords") or "").strip()[:255]
    row.question = str(payload.get("question") or "").strip()[:255]
    row.answer_guide = str(payload.get("answer_guide") or "").strip()
    row.sort_order = int(payload.get("sort_order") or 0)
    row.is_active = bool(payload.get("is_active", True))
    if not row.question or not row.answer_guide:
        raise ValueError("question and answer_guide are required")
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "topic": row.topic,
        "keywords": row.keywords,
        "question": row.question,
        "answer_guide": row.answer_guide,
        "sort_order": row.sort_order,
        "is_active": row.is_active,
    }


def delete_common_answer(db: Session, answer_id: int) -> None:
    row = db.query(CommonAnswers).filter(CommonAnswers.id == answer_id).first()
    if not row:
        raise ValueError("Common answer not found")
    db.delete(row)
    db.commit()


DEEPSEEK_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_hostel_info",
            "description": (
                "Get official check-in/out times, breakfast hours, parking, WhatsApp URL, "
                "address and email for Black Cat Hostal. "
                "If the guest is NOT allowed to book via API, use this and send WhatsApp."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_room_availability",
            "description": (
                "Check real FNSrooms availability (cupo) for Black Cat Hostal by dates. "
                "Use for authorized guests who want to reserve. Returns room_type_code, "
                "rate_plan_code and CLP booking fields "
                "(booking_amount_after_tax, booking_day_rates_json), plus guest prices "
                "in CLP only (guest_price_*_clp). Quote CLP to the guest; no USD. "
                "Persist bookings in CLP."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Check-in YYYY-MM-DD",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Check-out YYYY-MM-DD",
                    },
                    "adults": {"type": "integer", "description": "Adults (default 2)"},
                    "children": {"type": "integer", "description": "Children (default 0)"},
                    "currency": {
                        "type": "string",
                        "description": "Motor search currency for cupo; usually USD",
                    },
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_guest_bookings",
            "description": (
                "REQUIRED before create_room_booking whenever the guest gave "
                "RUT/passport OR first+last name while wanting to book. "
                "Finds unused active bookings. Prefer guest_document; else "
                "guest_first_name + guest_last_name. When matched_by=name, "
                "show booking_summaries and ask «¿es esa tu reserva?». "
                "If has_active_booking, ask: another, cancel, or change — "
                "do not silently create another."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "guest_document": {
                        "type": "string",
                        "description": "RUT (Chile) or passport number",
                    },
                    "guest_first_name": {"type": "string"},
                    "guest_last_name": {"type": "string"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_room_booking",
            "description": (
                "Cancel an existing FNSrooms booking after the guest confirms. "
                "Use booking_id from check_guest_bookings. "
                "If found by name only (no RUT), require guest said yes that "
                "the shown reservation is theirs → guest_confirmed=true. "
                "For changing dates/room prefer change_room_booking."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "booking_id": {
                        "type": "string",
                        "description": "Unique booking ID e.g. BC-XXXXXXXXXXXX",
                    },
                    "guest_confirmed": {
                        "type": "boolean",
                        "description": "true if guest confirmed the name-matched booking is theirs",
                    },
                    "guest_document": {"type": "string"},
                    "guest_first_name": {"type": "string"},
                    "guest_last_name": {"type": "string"},
                },
                "required": ["booking_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "change_room_booking",
            "description": (
                "Change an existing booking: find old by RUT/passport OR by "
                "first+last name, check NEW dates availability, cancel old, "
                "create new. If no document and matched by name, guest must "
                "confirm «sí, esa es mi reserva» → guest_confirmed=true. "
                "Call search_room_availability for new dates first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "room_type_code": {"type": "string"},
                    "rate_plan_code": {"type": "string"},
                    "amount_after_tax": {"type": "string"},
                    "guest_first_name": {"type": "string"},
                    "guest_last_name": {"type": "string"},
                    "guest_document": {
                        "type": "string",
                        "description": "RUT or passport if known",
                    },
                    "guest_confirmed": {
                        "type": "boolean",
                        "description": "Required true when changing without document after name verification",
                    },
                    "old_booking_id": {
                        "type": "string",
                        "description": "Optional; required if guest has several actives",
                    },
                    "guest_document_type": {"type": "string"},
                    "adults": {"type": "integer"},
                    "children": {"type": "integer"},
                    "units": {"type": "integer"},
                    "guest_email": {"type": "string"},
                    "guest_phone": {"type": "string"},
                    "remarks": {"type": "string"},
                    "day_rates_json": {"type": "string"},
                },
                "required": [
                    "start_date",
                    "end_date",
                    "room_type_code",
                    "rate_plan_code",
                    "amount_after_tax",
                    "guest_first_name",
                    "guest_last_name",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_room_booking",
            "description": (
                "Create a real FNSrooms booking. Only for authorized guests. "
                "Ask for first/last name AND RUT or passport. "
                "Call check_guest_bookings then search_room_availability first. "
                "Need guest_document, dates, room_type_code, rate_plan_code, and "
                "CLP amount_after_tax + day_rates_json from booking_* fields. "
                "currency must be CLP. Confirm CLP total only (no USD). "
                "If already_has_active_booking, ask: another, cancel, or change "
                "(change_room_booking). confirm_extra_booking=true only if insists "
                "on an additional booking."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "room_type_code": {"type": "string"},
                    "rate_plan_code": {"type": "string"},
                    "amount_after_tax": {"type": "string"},
                    "guest_first_name": {"type": "string"},
                    "guest_last_name": {"type": "string"},
                    "guest_document": {
                        "type": "string",
                        "description": "RUT (Chile) or passport number (required)",
                    },
                    "guest_document_type": {
                        "type": "string",
                        "description": "RUT or PASAPORTE (optional; inferred if empty)",
                    },
                    "currency": {
                        "type": "string",
                        "description": "Must be CLP to persist hostel prices in pesos",
                    },
                    "adults": {"type": "integer"},
                    "children": {"type": "integer"},
                    "units": {"type": "integer"},
                    "guest_email": {"type": "string"},
                    "guest_phone": {"type": "string"},
                    "remarks": {"type": "string"},
                    "confirm_extra_booking": {
                        "type": "boolean",
                        "description": (
                            "true only if guest already has a booking and confirmed "
                            "they want another one"
                        ),
                    },
                    "day_rates_json": {
                        "type": "string",
                        "description": (
                            'CLP night rates JSON, e.g. '
                            '[{"day":"2026-11-18","pnight":"19990.00"}]'
                        ),
                    },
                },
                "required": [
                    "start_date",
                    "end_date",
                    "room_type_code",
                    "rate_plan_code",
                    "amount_after_tax",
                    "guest_first_name",
                    "guest_last_name",
                    "guest_document",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_common_answers",
            "description": (
                "Lee las preguntas frecuentes. Úsala SIEMPRE para precios, tipos de habitación "
                "y qué incluye cada una: el campo answer ya trae esos datos. "
                "También sirve para WhatsApp, correo, horarios y dirección. "
                "Escribe esos valores en la respuesta, en el idioma del huésped. "
                "No uses el listado de habitaciones del panel. "
                "No contestes con instrucciones como 'comparte el WhatsApp'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Topic or keywords from the guest question",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the public web when the answer is not in the frequent questions "
                "or the hostel info. Use it for places in Santiago, public hours "
                "and facts that are not saved in the admin panel. "
                "Do not use it for room prices or room types."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query in the guest language or in Spanish",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_web_page",
            "description": (
                "Read the text of one HTTPS page returned by search_web. "
                "Do not open URLs that did not come from a tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "HTTPS URL from search_web results",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_directions_from_hostel",
            "description": (
                "Travel info from Black Cat Hostal to a place in Santiago. "
                "If the guest only asks how far / how long / how many minutes (eta_only=true), "
                "return car minutes only — no map, no metro detail. "
                "If they ask how to get there (eta_only=false), full route may include a map. "
                "Do not send them to WhatsApp."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "destination": {
                        "type": "string",
                        "description": "Place name, e.g. Palacio de La Moneda",
                    },
                    "eta_only": {
                        "type": "boolean",
                        "description": (
                            "True when asking distance/time only "
                            "(cuánto tiempo, qué tan lejos, how far). False for how-to-get."
                        ),
                    },
                },
                "required": ["destination"],
            },
        },
    },
]


def run_knowledge_tool(db: Session, name: str, arguments: dict[str, Any] | None = None) -> Any:
    args = arguments or {}
    if name == "search_rooms":
        return search_rooms(db, args.get("query") or args.get("consulta"))
    if name == "get_hostel_info":
        return hostel_info_dict(db)
    if name == "search_common_answers":
        return search_common_answers(db, args.get("query") or args.get("consulta"))
    if name == "get_directions_from_hostel":
        destination = str(args.get("destination") or args.get("lugar") or args.get("query") or "")
        eta_only = bool(args.get("eta_only") or args.get("solo_tiempo") or False)
        if eta_only:
            return driving_eta_from_hostel(destination)
        return directions_from_hostel(destination)
    return {"error": f"Unknown tool: {name}"}
