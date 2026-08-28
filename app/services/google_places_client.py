"""Google Places API (New) helpers — legacy Place Details/Find Place are blocked on new keys."""
from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings

PLACES_V1 = "https://places.googleapis.com/v1"


def places_headers(field_mask: str) -> dict[str, str]:
    return {
        "X-Goog-Api-Key": (settings.google_places_api_key or "").strip(),
        "X-Goog-FieldMask": field_mask,
        "Content-Type": "application/json",
    }


def place_resource_name(place_id: str) -> str:
    pid = (place_id or "").strip()
    if pid.startswith("places/"):
        return pid
    return f"places/{pid}"


def bare_place_id(place_id: str | None) -> str:
    if not place_id:
        return ""
    pid = place_id.strip()
    return pid.removeprefix("places/")


async def resolve_place_id(client: httpx.AsyncClient) -> str | None:
    configured = (settings.google_place_id or "").strip()
    if configured:
        return bare_place_id(configured)

    response = await client.post(
        f"{PLACES_V1}/places:searchText",
        headers=places_headers("places.id,places.displayName"),
        json={"textQuery": settings.google_place_query, "languageCode": "es"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    places = payload.get("places") or []
    if not places:
        return None
    return bare_place_id(places[0].get("id"))


async def fetch_place(
    client: httpx.AsyncClient,
    place_id: str,
    field_mask: str,
    language_code: str | None = None,
) -> dict[str, Any]:
    params = {"languageCode": language_code} if language_code else None
    response = await client.get(
        f"{PLACES_V1}/{place_resource_name(place_id)}",
        headers=places_headers(field_mask),
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


NEARBY_RESTAURANT_FIELDS = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.googleMapsUri,places.primaryType,places.rating,places.userRatingCount"
)

PLAZA_BRASIL_CENTER = {"latitude": -33.4399677, "longitude": -70.6691942}
PLAZA_BRASIL_RADIUS_M = 1200


def _language_code(locale: str) -> str:
    if locale.startswith("pt"):
        return "pt-BR"
    if locale.startswith("en"):
        return "en"
    return "es"


def _normalize_restaurant(place: dict[str, Any]) -> dict[str, Any]:
    location = place.get("location") or {}
    display = place.get("displayName") or {}
    name = display.get("text") if isinstance(display, dict) else str(display or "")
    lat = location.get("latitude")
    lng = location.get("longitude")
    return {
        "id": bare_place_id(place.get("id")),
        "name": name,
        "address": place.get("formattedAddress") or "",
        "lat": lat,
        "lng": lng,
        "googleMapsUri": place.get("googleMapsUri") or "",
        "primaryType": place.get("primaryType") or "restaurant",
        "rating": place.get("rating"),
        "reviewCount": place.get("userRatingCount"),
    }


async def search_nearby_restaurants(client: httpx.AsyncClient, locale: str = "es") -> list[dict[str, Any]]:
    language = _language_code(locale)
    circle = {"center": PLAZA_BRASIL_CENTER, "radius": PLAZA_BRASIL_RADIUS_M}
    headers = places_headers(NEARBY_RESTAURANT_FIELDS)

    nearby_response = await client.post(
        f"{PLACES_V1}/places:searchNearby",
        headers=headers,
        json={
            "includedPrimaryTypes": ["restaurant", "cafe", "bar", "bakery", "meal_takeaway"],
            "maxResultCount": 20,
            "locationRestriction": {"circle": circle},
            "languageCode": language,
            "regionCode": "cl",
            "rankPreference": "DISTANCE",
        },
        timeout=20,
    )
    nearby_response.raise_for_status()

    text_response = await client.post(
        f"{PLACES_V1}/places:searchText",
        headers=headers,
        json={
            "textQuery": "restaurantes cerca de Plaza Brasil Santiago",
            "maxResultCount": 20,
            "locationBias": {"circle": circle},
            "languageCode": language,
            "regionCode": "cl",
        },
        timeout=20,
    )
    text_response.raise_for_status()

    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for place in (nearby_response.json().get("places") or []) + (text_response.json().get("places") or []):
        normalized = _normalize_restaurant(place)
        pid = normalized.get("id")
        if not pid or pid in seen or not normalized.get("lat") or not normalized.get("lng"):
            continue
        seen.add(pid)
        results.append(normalized)
    return results
