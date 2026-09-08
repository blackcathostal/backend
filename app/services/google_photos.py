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

UPLOADS_DIR = settings.uploads_dir / "viajeros-google"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_ALBUM_URL = (
    "https://www.google.com/maps/place/HOSTAL+BOUTIQUE+BLACK+CAT/"
    "@-33.4397308,-70.6637911,17z/data=!4m9!3m8!1s0x9662c5b8477cf75b:0x9bc2ca30f81b6eff"
    "!5m2!4m1!1i2!8m2!3d-33.4397308!4d-70.6637911!16s%2Fg%2F11h190x96c"
)

OWNER_NAME_HINTS = (
    "black cat",
    "blackcat",
    "hostal boutique black cat",
    "hostal black cat",
)


def _album_url(place_id: str | None = None) -> str:
    configured = (getattr(settings, "google_photos_url", "") or "").strip()
    if configured:
        return configured
    if place_id:
        return f"https://www.google.com/maps/place/?q=place_id:{place_id}"
    return DEFAULT_ALBUM_URL


def _is_owner_attribution(names: list[str], business_name: str = "") -> bool:
    """True when the photo looks like a business/owner upload, not a guest."""
    if not names:
        return True
    business = (business_name or "").strip().lower()
    for raw in names:
        name = (raw or "").strip().lower()
        if not name:
            continue
        if business and business in name:
            return True
        if any(hint in name for hint in OWNER_NAME_HINTS):
            return True
    return False


def _local_photos() -> list[dict[str, Any]]:
    """
    Serve only the curated guest list in local.json when present.
    That avoids accidentally exposing hostel marketing dumps left as *.webp.
    """
    photos: list[dict[str, Any]] = []
    seen: set[str] = set()

    index = FRONTEND_DIR / "local.json"
    if index.exists():
        try:
            listed = json.loads(index.read_text(encoding="utf-8"))
        except Exception:
            listed = None
        if isinstance(listed, list):
            for raw in listed:
                name = Path(str(raw)).name
                if not name.endswith(".webp") or name in seen:
                    continue
                path = FRONTEND_DIR / name
                if not path.is_file():
                    upload = UPLOADS_DIR / name
                    if upload.is_file():
                        photos.append(
                            {
                                "id": upload.stem,
                                "url": f"/uploads/viajeros-google/{upload.name}",
                                "local": f"/uploads/viajeros-google/{upload.name}",
                                "source": "Google",
                                "kind": "customer",
                            }
                        )
                        seen.add(name)
                    continue
                seen.add(name)
                photos.append(
                    {
                        "id": path.stem,
                        "url": f"/cappa/img/viajeros-google/{path.name}",
                        "local": f"img/viajeros-google/{path.name}",
                        "source": "Google",
                        "kind": "customer",
                    }
                )
            return photos

    def add_from(folder: Path, url_prefix: str, local_prefix: str) -> None:
        if not folder.exists():
            return
        files = sorted(
            [
                p
                for p in folder.glob("*.webp")
                if p.is_file() and p.name not in {"local.webp", "favicon-preview.webp"}
            ],
            key=lambda p: p.name,
        )
        for path in files:
            if path.name in seen:
                continue
            seen.add(path.name)
            photos.append(
                {
                    "id": path.stem,
                    "url": f"{url_prefix}/{path.name}",
                    "local": f"{local_prefix}/{path.name}",
                    "source": "Google",
                    "kind": "customer",
                }
            )

    add_from(UPLOADS_DIR, "/uploads/viajeros-google", "/uploads/viajeros-google")
    add_from(FRONTEND_DIR, "/cappa/img/viajeros-google", "img/viajeros-google")
    return photos


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


def _write_local_index(photos: list[dict[str, Any]]) -> None:
    FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
    locals_ = [p["local"] for p in photos if str(p.get("local", "")).startswith("img/viajeros-google/")]
    if not locals_:
        locals_ = [f"img/viajeros-google/{Path(p['url']).name}" for p in photos if p.get("url")]
    (FRONTEND_DIR / "local.json").write_text(json.dumps(locals_, indent=2), encoding="utf-8")


async def sync_places_api_photos() -> dict[str, Any] | None:
    """
    Google Places (New) photos attributed to customers/guests only.
    Skips business/owner uploads (hostel marketing shots).
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
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

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
        business_name = ((result.get("displayName") or {}).get("text") or "").strip()
        if not refs:
            return _payload([], "google_places_api_empty", live=True, place_id=place_id)

        customer_refs: list[dict[str, Any]] = []
        for photo in refs:
            attributions = [
                a.get("displayName") or a.get("uri") or ""
                for a in (photo.get("authorAttributions") or [])
                if a
            ]
            if _is_owner_attribution(attributions, business_name):
                continue
            customer_refs.append({"photo": photo, "attributions": attributions})

        if not customer_refs:
            # Do not wipe existing customer sync with an empty/owner-only Places dump.
            local = _local_photos()
            if local:
                payload = _payload(local, "local_viajeros_google_kept", live=False, place_id=place_id)
                payload["note"] = "places_api_had_no_customer_attributed_photos"
                return payload
            return _payload([], "google_places_api_no_customers", live=True, place_id=place_id)

        for old in FRONTEND_DIR.glob("*.webp"):
            old.unlink()
        for old in UPLOADS_DIR.glob("*.webp"):
            old.unlink()

        saved: list[dict[str, Any]] = []
        for index, item in enumerate(customer_refs, 1):
            photo = item["photo"]
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
                image.save(FRONTEND_DIR / name, "WEBP", quality=85, method=6)
                image.save(UPLOADS_DIR / name, "WEBP", quality=85, method=6)
                saved.append(
                    {
                        "id": name,
                        "url": f"/cappa/img/viajeros-google/{name}",
                        "local": f"img/viajeros-google/{name}",
                        "source": "Google",
                        "kind": "customer",
                        "attributions": item["attributions"],
                    }
                )
            except Exception:
                continue

        _write_local_index(saved)
        return _payload(saved, "google_places_api_customers", live=True, place_id=place_id)


async def get_google_photos(force: bool = False) -> dict[str, Any]:
    """
    Prefer Places customer-attributed photos when the API key works.
    Otherwise serve locally synced Google Maps “De clientes” collage files.
    """
    if not force:
        cached = _read_cache()
        if cached and (cached.get("count") or 0) >= 1:
            return cached

    try:
        live = await sync_places_api_photos()
        if live is not None and (live.get("count") or 0) >= 1:
            _write_cache(live)
            return live
        if live is not None and live.get("live") and (live.get("count") or 0) == 0:
            # API worked but returned nothing useful — still try local customer sync.
            pass
    except Exception:
        stale = _read_cache(allow_stale=True)
        if stale and (stale.get("count") or 0) >= 1:
            stale = dict(stale)
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
