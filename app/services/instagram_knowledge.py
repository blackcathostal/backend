"""Knowledge lookup tools for DeepSeek Instagram replies (rooms, facts, FAQ)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.hostel_info import CommonAnswers, HostelInfo
from app.services.hostel_directions import directions_from_hostel
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


def _live_faq_answer(topic: str, guide: str, info: dict[str, Any], rooms: list[dict[str, Any]]) -> str:
    """Turn an internal instruction into the real hostel values."""
    stored = (guide or "").strip()
    if stored and not _is_instruction(stored):
        return stored
    whatsapp = str(info.get("whatsapp_url") or "").strip()
    email = str(info.get("email") or "").strip()
    address = str(info.get("address") or "").strip()
    check_in = str(info.get("check_in_time") or "").strip()
    check_out = str(info.get("check_out_time") or "").strip()
    breakfast = str(info.get("breakfast_hours") or "").strip()
    parking_notes = str(info.get("parking_notes") or "").strip()
    key = (topic or "").strip().lower()
    if key == "whatsapp":
        return f"Puedes escribirnos por WhatsApp {whatsapp} o al correo {email}."
    if key == "checkin":
        return (
            f"El check-in es a las {check_in}. "
            "Te enviamos las instrucciones de llegada antes de tu entrada."
        )
    if key == "checkout":
        return (
            f"El check-out es a las {check_out}. "
            f"Si necesitas salida más tarde, escríbenos por WhatsApp {whatsapp} para ver disponibilidad."
        )
    if key == "breakfast":
        hours = breakfast.replace("-", " a ", 1)
        return f"Sí, el desayuno continental está incluido. Se sirve de {hours}."
    if key == "parking":
        if info.get("has_parking"):
            return f"Sí, hay estacionamiento. {parking_notes}".strip()
        return parking_notes or "No hay estacionamiento en el hostal."
    if key == "location":
        return (
            f"Estamos en {address}. "
            f"Si necesitas ayuda para llegar, escríbenos por WhatsApp {whatsapp}."
        )
    if key == "prices":
        tariffs = [
            f"{room.get('name') or room.get('type')}: {room.get('price_label')} CLP"
            for room in rooms
            if room.get("price_label")
        ]
        listed = ". ".join(tariffs)
        extra = f" {listed}." if listed else ""
        return (
            "Las tarifas son referenciales y pueden variar por temporada."
            f"{extra} Para una fecha exacta escríbenos por WhatsApp {whatsapp}."
        )
    if key == "groups":
        return (
            "Sí, recibimos grupos y estadías corporativas. "
            f"Escríbenos por WhatsApp {whatsapp} con las fechas y la cantidad de personas para cotizar."
        )
    if key == "cancellation":
        return (
            "La cancelación depende de las condiciones de tu reserva. "
            f"Escríbenos por WhatsApp {whatsapp} o al correo {email} con el código de reserva."
        )
    if key == "availability":
        return (
            "Para confirmar disponibilidad necesitamos las fechas. "
            f"Escríbenos por WhatsApp {whatsapp}."
        )
    return stored


def _faq_payload(row: CommonAnswers, info: dict[str, Any], rooms: list[dict[str, Any]]) -> dict[str, Any]:
    answer = _live_faq_answer(row.topic, row.answer_guide, info, rooms)
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
            "name": "search_rooms",
            "description": (
                "Search the rooms table: type, price in CLP, capacity, features and status. "
                "ALWAYS use this when guests ask about prices, room types or what each room includes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional filter (e.g. double, family, twin, price)",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hostel_info",
            "description": (
                "Get official check-in/out times, breakfast hours, parking, WhatsApp URL, "
                "address and email for Black Cat Hostal."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_common_answers",
            "description": (
                "Lee las preguntas frecuentes. El campo answer ya trae los datos reales "
                "(WhatsApp, correo, horarios, dirección o precios). "
                "Escribe esos valores en la respuesta, en el idioma del huésped. "
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
            "name": "get_directions_from_hostel",
            "description": (
                "Walking route from Black Cat Hostal (Compañía de Jesús 1921, Barrio Brasil) "
                "to a place in Santiago, such as Palacio de La Moneda, a museum or a metro station. "
                "Use this when a guest asks how to get somewhere from the hostel. "
                "Answer with the returned duration, distance and steps. Do not send them to WhatsApp."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "destination": {
                        "type": "string",
                        "description": "Place name, e.g. Palacio de La Moneda",
                    }
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
        return directions_from_hostel(
            str(args.get("destination") or args.get("lugar") or args.get("query") or "")
        )
    return {"error": f"Unknown tool: {name}"}
