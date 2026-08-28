from __future__ import annotations

import hashlib
import json
import re
import time
from io import BytesIO
from typing import Any

import httpx
from PIL import Image

from app.services.google_photos import (
    FRONTEND_DIR,
    UPLOADS_DIR,
    _album_url,
    _local_photos,
    _payload,
    _write_cache,
    get_google_photos,
)

PHOTO_RE = re.compile(
    r"https://(?:lh\d+\.googleusercontent\.com|lh\d+\.ggpht\.com)/[^\s\"'<>\\]+",
    re.I,
)


def _normalize_url(url: str) -> str:
    url = (url or "").strip().replace("\\u002F", "/").replace("\\/", "/")
    if "googleusercontent.com" not in url and "ggpht.com" not in url:
        return ""
    low = url.lower()
    if any(x in low for x in ("=s36", "=s48", "=s64", "=s72", "=s96", "avatar", "profile")):
        return ""
    url = re.sub(r"=s\d+[^&\s]*", "=s1600", url, count=1, flags=re.I)
    url = re.sub(r"=w\d+-h\d+[^&\s]*", "=s1600", url, count=1, flags=re.I)
    return url.split()[0]


async def import_google_customer_photo_urls(urls: list[str]) -> dict[str, Any]:
    """Download Google user photo URLs into viajeros-google and refresh cache."""
    FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    unique: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        for match in PHOTO_RE.findall(raw) or ([raw] if raw else []):
            normalized = _normalize_url(match)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique.append(normalized)

    existing = {p.name for p in UPLOADS_DIR.glob("*.webp")} | {
        p.name for p in FRONTEND_DIR.glob("*.webp")
    }
    content_hashes: set[str] = set()
    for folder in (UPLOADS_DIR, FRONTEND_DIR):
        for path in folder.glob("*.webp"):
            try:
                content_hashes.add(hashlib.sha1(path.read_bytes()).hexdigest())
            except OSError:
                continue

    start = 1
    try:
        listed = json.loads((FRONTEND_DIR / "local.json").read_text(encoding="utf-8"))
        if isinstance(listed, list) and listed:
            start = len(listed) + 1
    except Exception:
        start = 1

    saved: list[dict[str, Any]] = []
    skipped = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.google.com/"},
        follow_redirects=True,
        timeout=40.0,
    ) as client:
        for url in unique:
            try:
                response = await client.get(url)
                if response.status_code != 200 or len(response.content) < 8000:
                    continue
                content_hash = hashlib.sha1(response.content).hexdigest()
                if content_hash in content_hashes:
                    skipped += 1
                    continue
                name = f"{start + len(saved):02d}.webp"
                while name in existing:
                    start += 1
                    name = f"{start + len(saved):02d}.webp"
                image = Image.open(BytesIO(response.content)).convert("RGB")
                width, height = image.size
                if max(width, height) < 240:
                    continue
                if max(width, height) > 1400:
                    scale = 1400 / max(width, height)
                    image = image.resize(
                        (int(width * scale), int(height * scale)),
                        Image.Resampling.LANCZOS,
                    )
                image.save(UPLOADS_DIR / name, "WEBP", quality=85, method=6)
                try:
                    image.save(FRONTEND_DIR / name, "WEBP", quality=85, method=6)
                except OSError:
                    pass
                existing.add(name)
                content_hashes.add(content_hash)
                saved.append(
                    {
                        "id": name,
                        "url": f"/cappa/img/viajeros-google/{name}",
                        "local": f"img/viajeros-google/{name}",
                        "source": "Google",
                        "kind": "customer",
                    }
                )
            except Exception:
                continue

    if saved:
        previous: list[str] = []
        try:
            raw = json.loads((FRONTEND_DIR / "local.json").read_text(encoding="utf-8"))
            if isinstance(raw, list):
                previous = [str(x) for x in raw]
        except Exception:
            previous = []
        for item in saved:
            loc = item["local"]
            if loc not in previous:
                previous.append(loc)
        (FRONTEND_DIR / "local.json").write_text(json.dumps(previous, indent=2), encoding="utf-8")

    locals_now = _local_photos()
    payload = _payload(locals_now, "google_customer_import", live=True)
    payload["album_url"] = _album_url()
    payload["imported"] = len(saved)
    payload["skipped"] = skipped
    payload["received"] = len(unique)
    payload["synced_at"] = int(time.time())
    _write_cache(payload)

    fresh = await get_google_photos(force=True)
    fresh["imported"] = len(saved)
    fresh["skipped"] = skipped
    fresh["received"] = len(unique)
    return fresh
