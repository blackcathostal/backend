"""Knowledge lookup tools for DeepSeek Instagram replies (rooms, facts, FAQ)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.hostel_info import CommonAnswers, HostelInfo
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
            "status": room.status,
            "features": getattr(room, "features", None) or "",
        }
        if needle:
            blob = " ".join(
                [
                    str(item["name"]),
                    str(item["type"]),
                    str(item["features"]),
                    str(item["status"]),
                ]
            ).lower()
            if needle not in blob:
                continue
        out.append(item)
    return out


def search_common_answers(db: Session, query: str | None = None) -> list[dict[str, Any]]:
    rows = (
        db.query(CommonAnswers)
        .filter(CommonAnswers.is_active.is_(True))
        .order_by(CommonAnswers.sort_order.asc(), CommonAnswers.id.asc())
        .all()
    )
    needle = (query or "").strip().lower()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = {
            "id": row.id,
            "topic": row.topic,
            "keywords": row.keywords,
            "question": row.question,
            "answer_guide": row.answer_guide,
        }
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
        return [
            {
                "id": row.id,
                "topic": row.topic,
                "keywords": row.keywords,
                "question": row.question,
                "answer_guide": row.answer_guide,
            }
            for row in rows
        ]
    return out


def list_common_answers_admin(db: Session) -> list[dict[str, Any]]:
    rows = db.query(CommonAnswers).order_by(CommonAnswers.sort_order.asc(), CommonAnswers.id.asc()).all()
    return [
        {
            "id": row.id,
            "topic": row.topic,
            "keywords": row.keywords,
            "question": row.question,
            "answer_guide": row.answer_guide,
            "sort_order": row.sort_order,
            "is_active": row.is_active,
        }
        for row in rows
    ]


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
                "Search common Instagram FAQ guides: breakfast, check-in, parking, location, "
                "contact, cancellations, groups, etc."
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
]


def run_knowledge_tool(db: Session, name: str, arguments: dict[str, Any] | None = None) -> Any:
    args = arguments or {}
    if name == "search_rooms":
        return search_rooms(db, args.get("query") or args.get("consulta"))
    if name == "get_hostel_info":
        return hostel_info_dict(db)
    if name == "search_common_answers":
        return search_common_answers(db, args.get("query") or args.get("consulta"))
    return {"error": f"Unknown tool: {name}"}
