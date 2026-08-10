from __future__ import annotations

import json
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from app.core.config import settings

CACHE_FILE = settings.uploads_dir / "cache" / "google_photos.json"
CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

FRONTEND_DIR = (
    Path(__file__).resolve().parents[3]
    / "frontend"
    / "public"
    / "cappa"
    / "img"
    / "viajeros-google"
)

DEFAULT_ALBUM_URL = (
    "https://www.google.com/maps/search/?api=1&query=Black+Cat+Hostal+Boutique+Santiago"
)


def _album_url(place_id: str | None = None) -> str:
    configured = (getattr(settings, "google_photos_url", "") or "").strip()
    if configured:
        return configured
    if place_id:
        return f"https://www.google.com/maps/place/?q=place_id:{place_id}"
    return DEFAULT_ALBUM_URL


def _local_photos() -> list[dict[str, Any]]:
    if not FRONTEND_DIR.exists():
        return []
    files = sorted(
        [p for p in FRONTEND_DIR.glob("*.webp") if p.name != "local.webp"],
        key=lambda p: p.name,
    )
    return [
        {
            "id": path.stem,
            "url": f"/cappa/img/viajeros-google/{path.name}",
            "local": f"img/viajeros-google/{path.name}",
            "source": "Google",
        }
        for path in files
    ]


def _payload(
    photos: list[dict[str, Any]],
    source: str,
    live: bool,
    place_id: str | None = None,
) -> dict[str, Any]:
    return {
        "total": len(photos),
        "count": len(photos),
        "photos": photos,
        "album_url": _album_url(place_id),
        "listing_url": _album_url(place_id),
        "source": source,
        "synced_at": int(time.time()),
        "live": live,
        "provider": "google",
        "place_id": place_id or "",
    }


def _read_cache(allow_stale: bool = False) -> dict[str, Any] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        age = int(time.time()) - int(data.get("synced_at", 0))
        ttl = getattr(settings, "google_photos_cache_seconds", 1800)
        if allow_stale or age <= ttl:
            return data
    except Exception:
        return None
    return None


def _write_cache(payload: dict[str, Any]) -> None:
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def sync_places_api_photos() -> dict[str, Any] | None:
    """
    Official Google Places (New) photos for this business only.
    Uses the same Places API key as Google Reviews.
    """
    from app.services.google_places_client import (
        PLACES_V1,
        fetch_place,
        resolve_place_id,
    )

    api_key = (settings.google_places_api_key or "").strip()
    if not api_key:
        return None

    FRONTEND_DIR.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=40.0, follow_redirects=True) as client:
        place_id = await resolve_place_id(client)
        if not place_id:
            return None

        result = await fetch_place(
            client,
            place_id,
            "id,displayName,googleMapsUri,photos",
        )
        refs = result.get("photos") or []
        if not refs:
            return _payload([], "google_places_api_empty", live=True, place_id=place_id)

        # Fresh sync: replace previous collage assets
        for old in FRONTEND_DIR.glob("*.webp"):
            old.unlink()

        saved: list[dict[str, Any]] = []
        for index, photo in enumerate(refs, 1):
            resource = (photo.get("name") or "").strip()
            if not resource:
                continue
            try:
                img_resp = await client.get(
                    f"{PLACES_V1}/{resource}/media",
                    params={"maxWidthPx": 1600, "key": api_key},
                    headers={"X-Goog-Api-Key": api_key},
                )
                if img_resp.status_code != 200 or len(img_resp.content) < 5000:
                    continue
                image = Image.open(BytesIO(img_resp.content)).convert("RGB")
                width, height = image.size
                if max(width, height) > 1400:
                    scale = 1400 / max(width, height)
                    image = image.resize(
                        (int(width * scale), int(height * scale)),
                        Image.Resampling.LANCZOS,
                    )
                name = f"{index:02d}.webp"
                dest = FRONTEND_DIR / name
                image.save(dest, "WEBP", quality=85, method=6)
                attributions = [
                    a.get("displayName") or a.get("uri") or ""
                    for a in (photo.get("authorAttributions") or [])
                    if a
                ]
                saved.append(
                    {
                        "id": name,
                        "url": f"/cappa/img/viajeros-google/{name}",
                        "local": f"img/viajeros-google/{name}",
                        "source": "Google",
                        "attributions": attributions,
                    }
                )
            except Exception:
                continue

        (FRONTEND_DIR / "local.json").write_text(
            json.dumps([p["local"] for p in saved], indent=2),
            encoding="utf-8",
        )
        return _payload(
            saved,
            "google_places_api",
            live=True,
            place_id=place_id,
        )


async def get_google_photos(force: bool = False) -> dict[str, Any]:
    """
    Prefer official Places API photos when GOOGLE_PLACES_API_KEY is set.
    Otherwise serve locally cached collage files if present.
    """
    if not force:
        cached = _read_cache()
        if cached and (cached.get("count") or 0) >= 1:
            return cached

    try:
        live = await sync_places_api_photos()
        if live is not None:
            _write_cache(live)
            return live
    except Exception:
        stale = _read_cache(allow_stale=True)
        if stale:
            stale["live"] = False
            stale["source"] = "cache"
            return stale

    local = _local_photos()
    if local:
        payload = _payload(local, "local_viajeros_google", live=False)
        _write_cache(payload)
        return payload

    empty = _payload([], "empty", live=False)
    _write_cache(empty)
    return empty
