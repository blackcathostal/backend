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
) -> dict[str, Any]:
    response = await client.get(
        f"{PLACES_V1}/{place_resource_name(place_id)}",
        headers=places_headers(field_mask),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()
