from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings

CACHE_FILE = settings.uploads_dir / "cache" / "tripadvisor_photos.json"
CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

# Local copies downloaded from Tripadvisor traveler album (albumid=107).
FRONTEND_TA_DIR = (
    Path(__file__).resolve().parents[3]
    / "frontend"
    / "public"
    / "cappa"
    / "img"
    / "viajeros-ta"
)

DEFAULT_ALBUM_URL = (
    "https://www.tripadvisor.cl/Hotel_Review-g294305-d18941046-Reviews-"
    "Hostal_Boutique_Black_Cat-Santiago_Santiago_Metropolitan_Region.html"
    "#/media/18941046/?type=TRAVELER&albumid=107&category=107"
)

DEFAULT_LISTING_URL = (
    "https://www.tripadvisor.cl/Hotel_Review-g294305-d18941046-Reviews-"
    "Hostal_Boutique_Black_Cat-Santiago_Santiago_Metropolitan_Region.html"
)

CURATED_TRAVELER_PHOTOS = [
    {
        "id": "19ba5d24",
        "url": (
            "https://dynamic-media-cdn.tripadvisor.com/media/photo-o/19/ba/5d/24/"
            "hostal-boutique-black.jpg?w=1200&h=900&s=1"
        ),
        "source": "Traveler",
    },
]


def _album_url() -> str:
    return settings.tripadvisor_photos_url or DEFAULT_ALBUM_URL


def _listing_url() -> str:
    return settings.tripadvisor_location_url or DEFAULT_LISTING_URL


def _local_traveler_photos() -> list[dict[str, Any]]:
    """Serve locally synced Tripadvisor traveler photos (auto-grows as files are added)."""
    photos: list[dict[str, Any]] = []
    if not FRONTEND_TA_DIR.exists():
        return photos

    manifest = FRONTEND_TA_DIR / "local.json"
    names: list[str] = []
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(data, list):
                names = [str(x).split("/")[-1] for x in data]
        except Exception:
            names = []

    files = sorted(
        [
            p
            for p in FRONTEND_TA_DIR.glob("*.webp")
            if p.is_file() and p.name not in {"favicon-preview.webp"}
        ],
        key=lambda p: p.name,
    )
    # Prefer numeric order from disk; append any missing from manifest.
    by_name = {p.name: p for p in files}
    ordered: list[Path] = []
    seen: set[str] = set()
    for name in names:
        if name in by_name and name not in seen:
            ordered.append(by_name[name])
            seen.add(name)
    for p in files:
        if p.name not in seen:
            ordered.append(p)
            seen.add(p.name)

    for path in ordered:
        photos.append(
            {
                "id": path.stem,
                "url": f"/cappa/img/viajeros-ta/{path.name}",
                "local": f"img/viajeros-ta/{path.name}",
                "source": "Traveler",
            }
        )
    return photos


def _merge_photos(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            url = (item.get("url") or "").split("?")[0]
            key = url or item.get("id") or ""
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _payload(photos: list[dict[str, Any]], source: str, live: bool) -> dict[str, Any]:
    return {
        "location_id": settings.tripadvisor_location_id or "18941046",
        "total": max(70, len(photos)),
        "count": len(photos),
        "photos": photos,
        "album_url": _album_url(),
        "listing_url": _listing_url(),
        "source": source,
        "synced_at": int(time.time()),
        "live": live,
        "provider": "tripadvisor",
    }


def _read_cache(allow_stale: bool = False) -> dict[str, Any] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        age = int(time.time()) - int(data.get("synced_at", 0))
        if allow_stale or age <= settings.tripadvisor_photos_cache_seconds:
            return data
    except Exception:
        return None
    return None


def _write_cache(payload: dict[str, Any]) -> None:
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _photo_from_api_item(item: dict[str, Any]) -> dict[str, Any] | None:
    images = item.get("images") or {}
    for size in ("original", "large", "medium", "small"):
        candidate = images.get(size) or {}
        url = candidate.get("url")
        if url:
            return {
                "id": str(item.get("id") or url),
                "url": url,
                "source": item.get("source") or "Traveler",
                "caption": (item.get("caption") or "").strip(),
            }
    return None


async def _fetch_content_api_photos() -> list[dict[str, Any]]:
    api_key = (settings.tripadvisor_api_key or "").strip()
    location_id = (settings.tripadvisor_location_id or "").strip()
    if not api_key or not location_id:
        return []

    photos: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    limit = 5

    async with httpx.AsyncClient(timeout=25.0) as client:
        while offset < 100:
            response = await client.get(
                f"https://api.content.tripadvisor.com/api/v1/location/{location_id}/photos",
                params={
                    "key": api_key,
                    "language": "es",
                    "limit": limit,
                    "offset": offset,
                    "source": "Traveler",
                },
            )
            if response.status_code != 200:
                break
            batch = response.json().get("data") or []
            if not batch:
                break
            for item in batch:
                parsed = _photo_from_api_item(item)
                if not parsed:
                    continue
                key = parsed["url"].split("?")[0]
                if key in seen:
                    continue
                seen.add(key)
                photos.append(parsed)
            if len(batch) < limit:
                break
            offset += limit

    return photos


async def get_tripadvisor_photos(force: bool = False) -> dict[str, Any]:
    """
    Traveler photos for collage:
    1) Tripadvisor Content API (source=Traveler) when API key is set — picks up new photos
    2) Local files in frontend/public/cappa/img/viajeros-ta from album sync
    3) Curated CDN fallback
    """
    local_photos = _local_traveler_photos()

    if not force:
        cached = _read_cache()
        if cached and (cached.get("count") or 0) >= max(6, len(local_photos)):
            # Prefer fresher local folder if it grew since last cache write.
            if len(local_photos) > int(cached.get("count") or 0):
                payload = _payload(local_photos, "local_viajeros_ta", live=True)
                _write_cache(payload)
                return payload
            return cached

    live_photos = await _fetch_content_api_photos()
    merged = _merge_photos(live_photos, local_photos)
    if merged:
        source = (
            "tripadvisor_content_api+local"
            if live_photos and local_photos
            else ("tripadvisor_content_api" if live_photos else "local_viajeros_ta")
        )
        payload = _payload(merged, source, live=bool(live_photos or len(local_photos) >= 2))
        _write_cache(payload)
        return payload

    stale = _read_cache(allow_stale=True)
    if stale and (stale.get("photos") or []):
        return stale

    payload = _payload(list(CURATED_TRAVELER_PHOTOS), "curated", live=False)
    _write_cache(payload)
    return payload
