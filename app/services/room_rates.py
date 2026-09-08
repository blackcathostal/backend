"""Official room rates table for Instagram DeepSeek replies."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.core.config import BASE_DIR, settings

DEFAULT_FILE = BASE_DIR / "app" / "data" / "instagram_room_rates.json"
CACHE_FILE = settings.uploads_dir / "cache" / "instagram_room_rates.json"


def _ensure_cache_dir() -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_room_rates() -> dict[str, Any]:
    _ensure_cache_dir()
    for path in (CACHE_FILE, DEFAULT_FILE):
        data = _read_json(path)
        if data and isinstance(data.get("rooms"), list):
            return data
    return {
        "currency": "CLP",
        "note": "Tarifas referenciales desde.",
        "includes": [],
        "contact": {"email": "reservas@blackcathostal.com"},
        "rooms": [],
    }


def save_room_rates(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Payload inválido")
    rooms = payload.get("rooms")
    if not isinstance(rooms, list) or not rooms:
        raise ValueError("Debe incluir al menos una habitación")
    cleaned_rooms: list[dict[str, Any]] = []
    for room in rooms:
        if not isinstance(room, dict):
            continue
        slug = str(room.get("slug") or "").strip()
        name_es = str(room.get("name_es") or "").strip()
        try:
            price = int(room.get("price_from_clp") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Precio inválido para {slug or name_es}") from exc
        if not slug or not name_es or price < 0:
            raise ValueError("Cada habitación necesita slug, name_es y price_from_clp")
        cleaned_rooms.append(
            {
                "slug": slug,
                "name_es": name_es,
                "name_en": str(room.get("name_en") or name_es).strip(),
                "capacity": str(room.get("capacity") or "").strip(),
                "price_from_clp": price,
            }
        )
    if not cleaned_rooms:
        raise ValueError("No hay habitaciones válidas")

    out = {
        "currency": str(payload.get("currency") or "CLP").strip() or "CLP",
        "note": str(payload.get("note") or "").strip(),
        "includes": [
            str(x).strip()
            for x in (payload.get("includes") or [])
            if str(x).strip()
        ],
        "contact": {
            "email": str((payload.get("contact") or {}).get("email") or "reservas@blackcathostal.com").strip(),
            "site": str((payload.get("contact") or {}).get("site") or "https://blackcathostal.com").strip(),
        },
        "rooms": cleaned_rooms,
    }
    _ensure_cache_dir()
    CACHE_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def format_rates_for_prompt(rates: dict[str, Any] | None = None) -> str:
    data = rates or load_room_rates()
    lines = [
        f"Moneda: {data.get('currency') or 'CLP'}",
        f"Nota: {data.get('note') or ''}",
        "Incluye (orientación): " + ", ".join(data.get("includes") or []) ,
        "Tabla de tarifas desde:",
    ]
    for room in data.get("rooms") or []:
        price = int(room.get("price_from_clp") or 0)
        price_fmt = f"${price:,}".replace(",", ".")
        lines.append(
            f"- {room.get('name_es')} ({room.get('slug')}): desde {price_fmt} CLP"
            f" | capacidad: {room.get('capacity') or 'n/d'}"
            f" | EN: {room.get('name_en') or room.get('name_es')}"
        )
    contact = data.get("contact") or {}
    lines.append(
        f"Contacto reservas: {contact.get('email') or 'reservas@blackcathostal.com'}"
        f" | web: {contact.get('site') or 'https://blackcathostal.com'}"
    )
    return "\n".join(lines)


def rates_public_payload() -> dict[str, Any]:
    return deepcopy(load_room_rates())
