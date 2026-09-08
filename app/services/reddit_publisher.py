"""Publish blog posts to Reddit via OAuth (script app / password grant)."""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.core.config import settings

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
SUBMIT_URL = "https://oauth.reddit.com/api/submit"

_cached_token: str | None = None
_cached_until: float = 0.0


class RedditConfigError(Exception):
    pass


class RedditPublishError(Exception):
    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.details = details


def reddit_configured() -> bool:
    return bool(
        (settings.reddit_client_id or "").strip()
        and (settings.reddit_client_secret or "").strip()
        and (settings.reddit_username or "").strip()
        and (settings.reddit_password or "").strip()
    )


def reddit_status() -> dict[str, Any]:
    return {
        "configured": reddit_configured(),
        "username": (settings.reddit_username or "").strip() or None,
        "default_subreddit": (settings.reddit_default_subreddit or "Chile").strip() or "Chile",
        "user_agent": (settings.reddit_user_agent or "").strip() or None,
        "hint": (
            None
            if reddit_configured()
            else (
                "Configura REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, "
                "REDDIT_USERNAME y REDDIT_PASSWORD en backend/.env "
                "(app tipo script en https://www.reddit.com/prefs/apps)."
            )
        ),
    }


def _user_agent() -> str:
    return (
        (settings.reddit_user_agent or "").strip()
        or f"BlackCatHostalAdmin/1.0 (by /u/{settings.reddit_username or 'BlackCatHostal'})"
    )


async def get_access_token(force: bool = False) -> str:
    global _cached_token, _cached_until
    if not force and _cached_token and time.time() < _cached_until - 60:
        return _cached_token

    if not reddit_configured():
        raise RedditConfigError(
            "Reddit no está configurado. Agrega las variables REDDIT_* en el .env del backend."
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            TOKEN_URL,
            auth=(
                settings.reddit_client_id.strip(),
                settings.reddit_client_secret.strip(),
            ),
            data={
                "grant_type": "password",
                "username": settings.reddit_username.strip(),
                "password": settings.reddit_password.strip(),
            },
            headers={"User-Agent": _user_agent()},
        )

    payload = response.json() if response.content else {}
    if response.status_code >= 400 or "access_token" not in payload:
        raise RedditPublishError(
            payload.get("error_description")
            or payload.get("message")
            or f"No se pudo autenticar en Reddit ({response.status_code})",
            details=payload,
        )

    _cached_token = str(payload["access_token"])
    expires = int(payload.get("expires_in") or 3600)
    _cached_until = time.time() + expires
    return _cached_token


def build_post_url(slug: str) -> str:
    base = (settings.public_site_url or "https://blackcathostal.com").rstrip("/")
    return f"{base}/post/{slug}"


def build_default_text(*, title: str, excerpt: str, url: str) -> str:
    parts = []
    if excerpt.strip():
        parts.append(excerpt.strip())
    parts.append(f"\nMás detalles: {url}")
    parts.append("\n— Black Cat Hostal Boutique (Barrio Brasil, Santiago)")
    body = "\n".join(parts).strip()
    # Reddit self-text soft limit ~40k; keep practical.
    return body[:39000]


async def submit_post(
    *,
    subreddit: str,
    title: str,
    kind: str = "link",
    text: str = "",
    url: str = "",
    nsfw: bool = False,
    send_replies: bool = True,
) -> dict[str, Any]:
    sr = (subreddit or "").strip().lstrip("r/")
    if not sr:
        raise RedditPublishError("Indica un subreddit")
    clean_title = (title or "").strip()
    if not clean_title:
        raise RedditPublishError("El título es obligatorio")
    if len(clean_title) > 300:
        raise RedditPublishError("El título de Reddit no puede superar 300 caracteres")

    kind = (kind or "link").strip().lower()
    if kind not in {"link", "self"}:
        raise RedditPublishError("kind debe ser 'link' o 'self'")

    token = await get_access_token()
    data: dict[str, Any] = {
        "api_type": "json",
        "kind": kind,
        "sr": sr,
        "title": clean_title,
        "nsfw": "true" if nsfw else "false",
        "sendreplies": "true" if send_replies else "false",
        "resubmit": "true",
    }
    if kind == "link":
        if not (url or "").strip():
            raise RedditPublishError("Para un post tipo link se necesita la URL del artículo")
        data["url"] = url.strip()
    else:
        data["text"] = (text or "").strip() or clean_title

    async with httpx.AsyncClient(timeout=40.0) as client:
        response = await client.post(
            SUBMIT_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": _user_agent(),
            },
            data=data,
        )

    payload = response.json() if response.content else {}
    if response.status_code >= 400:
        raise RedditPublishError(
            f"Reddit respondió {response.status_code}",
            details=payload,
        )

    errors = (((payload.get("json") or {}).get("errors")) or [])
    if errors:
        # errors is list of [name, message, field]
        message = "; ".join(
            str(item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else item)
            for item in errors
        )
        raise RedditPublishError(message or "Reddit rechazó la publicación", details=payload)

    result = ((payload.get("json") or {}).get("data")) or {}
    return {
        "ok": True,
        "subreddit": sr,
        "kind": kind,
        "title": clean_title,
        "url": result.get("url") or result.get("permalink"),
        "id": result.get("id") or result.get("name"),
        "raw": result,
    }
