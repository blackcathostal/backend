"""Short Google Maps links for Instagram (bit.ly or /api/g/xxxx redirect)."""

from __future__ import annotations

import json
import secrets
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


def _public_short_origin() -> str:
    redirect = str(settings.meta_redirect_uri or "").strip()
    parsed = urlparse(redirect)
    if parsed.scheme == "https" and parsed.netloc and "localhost" not in parsed.netloc.lower():
        return f"{parsed.scheme}://{parsed.netloc}"
    return "https://api.blackcathostal.com"


def create_maps_redirect(url: str) -> str:
    """Store a short /api/g/xxxx link that 302-redirects to Google Maps."""
    target = str(url or "").strip()
    if not target.startswith("https://") or not _maps_host(target):
        return ""
    store = _load()
    for code, saved in store.items():
        if saved == target and 4 <= len(code) <= 8:
            return f"{_public_short_origin()}/api/g/{code}"
    code = ""
    for _ in range(12):
        candidate = secrets.token_hex(3)  # 6 chars
        if candidate not in store:
            code = candidate
            break
    if not code:
        return ""
    store[code] = target
    path = _store_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return f"{_public_short_origin()}/api/g/{code}"


def shorten_url(url: str) -> str:
    """Prefer bit.ly; otherwise a short /api/g/ redirect that opens Google Maps."""
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
    if token:
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
            link = ""
        if _is_bitly(link):
            cached[target] = link
            path = _bitly_path()
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
            return link
    return create_maps_redirect(target)


def resolve_short_link(code: str) -> str:
    token = str(code or "").strip().lower()
    if len(token) < 4 or len(token) > 8:
        return ""
    target = _load().get(token) or ""
    if target.startswith("https://") and _maps_host(target):
        return target
    return ""


def ver_mapa_message(maps_url: str, *, language: str = "es") -> str:
    """Short tap target for Instagram: label + clickable short URL."""
    url = str(maps_url or "").strip()
    if not url.startswith("https://"):
        return ""
    if language == "en":
        return f"Open map\n{url}"
    if language == "pt":
        return f"Ver mapa\n{url}"
    return f"Ver mapa\n{url}"
