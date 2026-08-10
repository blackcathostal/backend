"""Validate Google Places API key and print place_id + review/photo counts."""
from __future__ import annotations

import asyncio
import sys

from app.core.config import settings
from app.services.google_photos import get_google_photos
from app.services.google_reviews import get_google_reviews


async def main() -> None:
    key = (settings.google_places_api_key or "").strip()
    if not key:
        print("Falta GOOGLE_PLACES_API_KEY en backend/.env")
        print("Copia backend/.env.example -> backend/.env y pega tu key.")
        sys.exit(1)

    print("key:", key[:8] + "…" + key[-4:])
    print("place_id config:", settings.google_place_id or "(auto)")
    print("query:", settings.google_place_query)

    reviews = await get_google_reviews(force=True)
    print(
        "reviews:",
        reviews.get("source"),
        "live=",
        reviews.get("live"),
        "rating=",
        reviews.get("rating"),
        "total=",
        reviews.get("user_ratings_total"),
        "items=",
        len(reviews.get("reviews") or []),
        "place_id=",
        reviews.get("place_id"),
    )

    photos = await get_google_photos(force=True)
    print(
        "photos:",
        photos.get("source"),
        "live=",
        photos.get("live"),
        "count=",
        photos.get("count"),
        "place_id=",
        photos.get("place_id"),
    )


if __name__ == "__main__":
    asyncio.run(main())
