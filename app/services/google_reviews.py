from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings

CACHE_FILE = settings.uploads_dir / "cache" / "google_reviews.json"
CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

FALLBACK_PLACE_URL = (
    "https://www.google.com/maps/search/?api=1&query=Black+Cat+Hostal+Compa%C3%B1%C3%ADa+de+Jes%C3%BAs+1921+Santiago"
)


def _language_code(locale: str) -> str:
    normalized = (locale or "es").lower()
    if normalized.startswith("pt"):
        return "pt-BR"
    if normalized.startswith("en"):
        return "en"
    return "es"


def _cache_file(locale: str) -> Path:
    suffix = _language_code(locale).lower().replace("-", "_")
    return CACHE_FILE.with_name(f"google_reviews_{suffix}.json")


# Real guest comments from blackcathostal.com (used until Google API is configured)
FALLBACK_REVIEWS = [
    {
        "author_name": "María Paz Contreras",
        "rating": 5,
        "text": (
            "Excelente ubicación, instalaciones de primer nivel, cómodo ambiente familiar "
            "y la atención de muy buena calidad. Muy conforme con la visita. Volveré sin duda."
        ),
        "time": int(time.time()) - 86400 * 3,
        "relative_time_description": "hace 3 días",
        "profile_photo_url": "",
        "author_url": "",
    },
    {
        "author_name": "Diego Morales",
        "rating": 5,
        "text": (
            "Todo se cumplió de manera óptima: instalaciones de primer nivel en comodidad, "
            "limpieza y ambiente. El staff respondió con excelente disposición."
        ),
        "time": int(time.time()) - 86400 * 8,
        "relative_time_description": "hace 1 semana",
        "profile_photo_url": "",
        "author_url": "",
    },
    {
        "author_name": "Camila Riquelme",
        "rating": 5,
        "text": (
            "Fuimos por San Valentín con mi pareja y nuestro bebé, y la experiencia fue maravillosa. "
            "Todo el equipo fue simpático y atento. El lugar es bellísimo y con mucho estilo."
        ),
        "time": int(time.time()) - 86400 * 15,
        "relative_time_description": "hace 2 semanas",
        "profile_photo_url": "",
        "author_url": "",
    },
    {
        "author_name": "Andrés Lefever",
        "rating": 5,
        "text": (
            "100% recomendado, cómodo, seguro y excelente atención. Las habitaciones están nuevas "
            "y en excelente estado. Ideal para familia o amigos."
        ),
        "time": int(time.time()) - 86400 * 28,
        "relative_time_description": "hace 1 mes",
        "profile_photo_url": "",
        "author_url": "",
    },
    {
        "author_name": "Valentina Núñez",
        "rating": 5,
        "text": (
            "Experiencia muy linda, lugar bonito y con muy buena ubicación. Habitación cómoda y "
            "bien equipada, atención excelente y desayuno rico y variado."
        ),
        "time": int(time.time()) - 86400 * 40,
        "relative_time_description": "hace 1 mes",
        "profile_photo_url": "",
        "author_url": "",
    },
]


def _fallback_payload(source: str = "fallback") -> dict[str, Any]:
    reviews = sorted(FALLBACK_REVIEWS, key=lambda item: item["time"], reverse=True)
    return {
        "name": "Black Cat Hostal",
        "rating": 4.9,
        "user_ratings_total": 186,
        "url": FALLBACK_PLACE_URL,
        "write_review_url": FALLBACK_PLACE_URL,
        "reviews": reviews,
        "source": source,
        "synced_at": int(time.time()),
        "live": False,
    }


def _read_cache(locale: str) -> dict[str, Any] | None:
    cache_file = _cache_file(locale)
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        age = int(time.time()) - int(data.get("synced_at", 0))
        if age <= settings.google_reviews_cache_seconds:
            return data
    except Exception:
        return None
    return None


def _write_cache(payload: dict[str, Any], locale: str) -> None:
    _cache_file(locale).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_reviews(raw_reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    for item in raw_reviews:
        text_value = item.get("text") or item.get("originalText") or ""
        if isinstance(text_value, dict):
            text_value = text_value.get("text") or ""
        author_attribution = item.get("authorAttribution") or {}
        reviews.append(
            {
                "author_name": item.get("author_name") or author_attribution.get("displayName") or "Huésped",
                "rating": int(item.get("rating") or 5),
                "text": text_value,
                "time": int(item.get("time") or 0),
                "relative_time_description": item.get("relative_time_description")
                or item.get("relativePublishTimeDescription")
                or "",
                "profile_photo_url": item.get("profile_photo_url")
                or author_attribution.get("photoUri")
                or "",
                "author_url": item.get("author_url")
                or author_attribution.get("uri")
                or "",
            }
        )
    reviews.sort(key=lambda review: review.get("time") or 0, reverse=True)
    return [review for review in reviews if review.get("text")]


async def _fetch_from_google(locale: str) -> dict[str, Any] | None:
    from app.services.google_places_client import fetch_place, resolve_place_id

    api_key = (settings.google_places_api_key or "").strip()
    if not api_key:
        return None

    async with httpx.AsyncClient() as client:
        place_id = await resolve_place_id(client)
        if not place_id:
            return None

        result = await fetch_place(
            client,
            place_id,
            "id,displayName,rating,userRatingCount,reviews,googleMapsUri",
            language_code=_language_code(locale),
        )
        display = result.get("displayName") or {}
        reviews = _normalize_reviews(result.get("reviews") or [])
        return {
            "name": display.get("text") or "Black Cat Hostal",
            "rating": float(result.get("rating") or 0),
            "user_ratings_total": int(result.get("userRatingCount") or len(reviews)),
            "url": result.get("googleMapsUri") or FALLBACK_PLACE_URL,
            "write_review_url": f"https://search.google.com/local/writereview?placeid={place_id}",
            "reviews": reviews,
            "source": "google",
            "synced_at": int(time.time()),
            "live": True,
            "place_id": place_id,
        }


async def get_google_reviews(force: bool = False, locale: str = "es") -> dict[str, Any]:
    if not force:
        cached = _read_cache(locale)
        if cached:
            return cached

    try:
        live = await _fetch_from_google(locale)
        if live and live.get("reviews"):
            _write_cache(live, locale)
            return live
    except Exception:
        # Keep serving cache/fallback if Google is unavailable
        stale = None
        cache_file = _cache_file(locale)
        if cache_file.exists():
            try:
                stale = json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:
                stale = None
        if stale:
            stale["live"] = False
            stale["source"] = "cache"
            return stale

    payload = _fallback_payload()
    _write_cache(payload, locale)
    return payload
