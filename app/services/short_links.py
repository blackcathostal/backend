"""Turn a long Google Maps URL into a bit.ly link. Never publish the API host."""

from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx

from app.core.config import settings

def _store_path():
    path = settings.uploads_dir / "cache" / "short_links.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load() -> dict[str, str]:
    path = _store_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if key and value}


def _maps_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "google.com" or host.endswith(".google.com") or host == "goo.gl" or host.endswith(".goo.gl")


def _bitly_path():
    path = settings.uploads_dir / "cache" / "bitly_links.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _bitly_load() -> dict[str, str]:
    path = _bitly_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if key and value}


def _is_bitly(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "bit.ly" or host == "bitly.com" or host.endswith(".bitly.com")


def shorten_url(url: str) -> str:
    """Return a bit.ly link. An empty string means the caption must not show a URL."""
    target = str(url or "").strip()
    if _is_bitly(target):
        return target
    if not target.startswith("https://") or not _maps_host(target):
        return ""
    cached = _bitly_load()
    saved = cached.get(target) or ""
    if _is_bitly(saved):
        return saved
    token = (settings.bitly_access_token or "").strip()
    if not token:
        return ""
    try:
        response = httpx.post(
            "https://api-ssl.bitly.com/v4/shorten",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"long_url": target, "domain": "bit.ly"},
            timeout=12.0,
        )
        response.raise_for_status()
        link = str(response.json().get("link") or "")
    except (httpx.HTTPError, ValueError):
        return ""
    if not _is_bitly(link):
        return ""
    cached[target] = link
    path = _bitly_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return link


def resolve_short_link(code: str) -> str:
    token = str(code or "").strip().lower()
    if len(token) < 4 or len(token) > 8:
        return ""
    target = _load().get(token) or ""
    if target.startswith("https://") and _maps_host(target):
        return target
    return ""
