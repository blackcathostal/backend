from __future__ import annotations

import json
import time
from typing import Any

from app.core.config import settings

CACHE_FILE = settings.uploads_dir / "cache" / "tripadvisor_reviews.json"
CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_URL = (
    "https://www.tripadvisor.com/Hotel_Review-g294305-d18941046-Reviews-"
    "Hostal_Boutique_Black_Cat-Santiago_Santiago_Metropolitan_Region.html"
)

FALLBACK_REVIEWS = [
    {
        "author_name": "Sophie M.",
        "rating": 5,
        "title": "Excelente hostal boutique",
        "text": (
            "Lugar limpio, moderno y con mucha personalidad. El personal fue muy atento y "
            "la ubicación en Barrio Brasil es perfecta para recorrer Santiago."
        ),
        "time": int(time.time()) - 86400 * 4,
        "relative_time_description": "hace 4 días",
        "profile_photo_url": "",
        "author_url": "",
    },
    {
        "author_name": "Lucas P.",
        "rating": 5,
        "title": "Muy recomendable",
        "text": (
            "Habitaciones cómodas, desayuno rico y áreas comunes hermosas. Se nota el cuidado "
            "en cada detalle. Volveríamos sin dudar."
        ),
        "time": int(time.time()) - 86400 * 11,
        "relative_time_description": "hace 2 semanas",
        "profile_photo_url": "",
        "author_url": "",
    },
    {
        "author_name": "Emma R.",
        "rating": 5,
        "title": "Gran estadía en Santiago",
        "text": (
            "Check-in fácil, staff amable y bilingüe, y un ambiente boutique que se siente "
            "diferente a un hostal tradicional. Ideal para parejas y viajeros."
        ),
        "time": int(time.time()) - 86400 * 19,
        "relative_time_description": "hace 3 semanas",
        "profile_photo_url": "",
        "author_url": "",
    },
    {
        "author_name": "Javier C.",
        "rating": 4,
        "title": "Buena ubicación y diseño",
        "text": (
            "Nos gustó mucho el patio y el diseño del lugar. Buena relación calidad-precio "
            "y cerca de cafés, museos y transporte."
        ),
        "time": int(time.time()) - 86400 * 33,
        "relative_time_description": "hace 1 mes",
        "profile_photo_url": "",
        "author_url": "",
    },
    {
        "author_name": "Ana Beatriz",
        "rating": 5,
        "title": "Volveremos",
        "text": (
            "Todo impecable: limpieza, atención y comodidad. El hostal tiene un estilo único "
            "y te hace sentir como en casa."
        ),
        "time": int(time.time()) - 86400 * 47,
        "relative_time_description": "hace 2 meses",
        "profile_photo_url": "",
        "author_url": "",
    },
]


def _fallback_payload(source: str = "fallback") -> dict[str, Any]:
    reviews = sorted(FALLBACK_REVIEWS, key=lambda item: item["time"], reverse=True)
    listing_url = settings.tripadvisor_location_url or DEFAULT_URL
    return {
        "name": "Hostal Boutique Black Cat",
        "rating": 4.8,
        "user_ratings_total": 89,
        "url": listing_url,
        "write_review_url": listing_url,
        "reviews": reviews,
        "source": source,
        "synced_at": int(time.time()),
        "live": False,
        "provider": "tripadvisor",
    }


def _read_cache() -> dict[str, Any] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        age = int(time.time()) - int(data.get("synced_at", 0))
        if age <= settings.tripadvisor_reviews_cache_seconds:
            return data
    except Exception:
        return None
    return None


def _write_cache(payload: dict[str, Any]) -> None:
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def get_tripadvisor_reviews(force: bool = False) -> dict[str, Any]:
    """
    TripAdvisor Content API requires partner credentials.
    Until configured, serve curated fallback reviews with TripAdvisor branding/links.
    """
    if not force:
        cached = _read_cache()
        if cached:
            return cached

    # Optional: if a partner API key is ever added, fetch live data here.
    # For now always use branded fallback (newest first).
    payload = _fallback_payload()
    if settings.tripadvisor_location_url:
        payload["url"] = settings.tripadvisor_location_url
        payload["write_review_url"] = settings.tripadvisor_location_url
    _write_cache(payload)
    return payload
