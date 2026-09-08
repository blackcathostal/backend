from __future__ import annotations

import httpx

from app.core.config import settings
from app.services.google_places_client import search_nearby_restaurants


async def get_nearby_restaurants(locale: str = "es") -> dict:
    api_key = (settings.google_places_api_key or "").strip()
    if not api_key:
        return {"places": [], "live": False, "error": "missing_api_key"}

    try:
        async with httpx.AsyncClient() as client:
            places = await search_nearby_restaurants(client, locale=locale)
        return {"places": places, "live": True, "count": len(places)}
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300] if exc.response is not None else str(exc)
        return {"places": [], "live": False, "error": "google_places_http_error", "detail": detail}
    except Exception as exc:
        return {"places": [], "live": False, "error": "google_places_error", "detail": str(exc)}
