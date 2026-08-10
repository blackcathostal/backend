from __future__ import annotations

import hashlib
import json
import re
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from app.core.config import settings
from app.services.tripadvisor_photos import (
    FRONTEND_TA_DIR,
    _album_url,
    _listing_url,
    _local_traveler_photos,
    _payload,
    _write_cache,
    get_tripadvisor_photos,
)

PHOTO_HOST = "media-cdn.tripadvisor.com"
PHOTO_RE = re.compile(
    r"https://(?:dynamic-)?media-cdn\.tripadvisor\.com/media/photo-[ost]/[^\s\"'<>\\]+",
    re.I,
)


def _normalize_url(url: str) -> str:
    url = (url or "").strip().replace("\\u002F", "/").replace("\\/", "/")
    url = url.split("?")[0]
    url = url.replace("https://media-cdn.tripadvisor.com", "https://dynamic-media-cdn.tripadvisor.com")
    if PHOTO_HOST not in url or "/media/photo-" not in url:
        return ""
    low = url.lower()
    if any(x in low for x in ("logo", "avatar", "icon", "sprite", "badge")):
        return ""
    return url


async def import_traveler_photo_urls(urls: list[str]) -> dict[str, Any]:
    """Download Tripadvisor CDN traveler photos into viajeros-ta and refresh cache."""
    FRONTEND_TA_DIR.mkdir(parents=True, exist_ok=True)
    unique: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        for match in PHOTO_RE.findall(raw) or ([raw] if raw else []):
            normalized = _normalize_url(match)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique.append(normalized)

    existing = {p.name for p in FRONTEND_TA_DIR.glob("*.webp")}
    saved: list[dict[str, Any]] = []
    skipped = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0", "Referer": _listing_url()},
        follow_redirects=True,
        timeout=40.0,
    ) as client:
        for url in unique:
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
            name = f"ta-{digest}.webp"
            if name in existing:
                skipped += 1
                continue
            try:
                response = await client.get(f"{url}?w=1200&h=1200&s=1")
                if response.status_code != 200 or len(response.content) < 4000:
                    continue
                image = Image.open(BytesIO(response.content)).convert("RGB")
                width, height = image.size
                if max(width, height) > 1400:
                    scale = 1400 / max(width, height)
                    image = image.resize(
                        (int(width * scale), int(height * scale)),
                        Image.Resampling.LANCZOS,
                    )
                dest = FRONTEND_TA_DIR / name
                image.save(dest, "WEBP", quality=85, method=6)
                existing.add(name)
                saved.append(
                    {
                        "id": digest,
                        "url": f"{url}?w=1200&h=1200&s=1",
                        "local": f"img/viajeros-ta/{name}",
                        "source": "Traveler",
                    }
                )
            except Exception:
                continue

    # Refresh local.json for stable ordering
    locals_now = _local_traveler_photos()
    (FRONTEND_TA_DIR / "local.json").write_text(
        json.dumps([p.get("local") for p in locals_now], indent=2),
        encoding="utf-8",
    )

    payload = _payload(locals_now, "tripadvisor_import", live=True)
    payload["album_url"] = _album_url()
    payload["imported"] = len(saved)
    payload["skipped"] = skipped
    payload["received"] = len(unique)
    payload["synced_at"] = int(time.time())
    _write_cache(payload)

    # Return fresh view
    fresh = await get_tripadvisor_photos(force=True)
    fresh["imported"] = len(saved)
    fresh["skipped"] = skipped
    fresh["received"] = len(unique)
    return fresh
