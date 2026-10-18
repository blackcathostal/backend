"""Create one Instagram feed post from a web page, using MCP tools."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.services.deepseek_instagram import (
    DeepSeekError,
    _extract_json,
    _require_deepseek,
)
from app.services.instagram import (
    InstagramError,
    _graph_get,
    _graph_post,
    _require_connection,
)
from app.services.mcp_sources import call_reply_tool

_SEARCHES = (
    "qué hacer en Santiago centro Barrio Brasil",
    "turismo Santiago centro",
)

_SYSTEM = """Eres el community manager de Black Cat Hostal Boutique, en Compañía de Jesús 1921, Barrio Brasil, Santiago.
Escribes el texto de un post de Instagram a partir de una página que ya fue leída.
El mensaje va en español, claro y corto, para quien visita Santiago.
No copies la página entera. No inventes precios, horarios ni datos que no estén en el texto.
Al final cita la fuente con su nombre y su URL.
Responde solo JSON: {"caption":"..."}
"""


def _with_source(caption: str, source_name: str, source_url: str) -> str:
    text = caption.strip()
    if source_url not in text:
        label = source_name.strip() or "Fuente"
        text = f"{text}\n\nFuente: {label}\n{source_url}"
    return text[:2200]


async def _page_with_media() -> dict[str, Any]:
    """Search and open pages through MCP until one has a photo or a video."""
    seen: set[str] = set()
    for query in _SEARCHES:
        found = await call_reply_tool("search_web", {"query": query, "max_results": 4})
        for row in (found.get("results") or [])[:4]:
            url = str(row.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            media = await call_reply_tool("find_web_media", {"url": url})
            if not isinstance(media, dict) or not media.get("ok"):
                continue
            videos = [str(item.get("url") or "") for item in media.get("videos") or [] if item.get("url")]
            images = [str(item.get("url") or "") for item in media.get("images") or [] if item.get("url")]
            if videos or images:
                media["chosen_video"] = videos[0] if videos else ""
                media["chosen_image"] = images[0] if images else ""
                return media
    raise InstagramError(
        "No encontré en la web una foto o un video que Instagram pueda publicar. Intenta de nuevo."
    )


async def _write_caption(page: dict[str, Any]) -> str:
    base, model, headers = _require_deepseek()
    source_url = str(page.get("url") or "").strip()
    source_name = str(page.get("source_name") or page.get("title") or "").strip()
    kind = "video" if page.get("chosen_video") else "foto"
    user = (
        f"Escribe el texto del post. El archivo es una {kind}.\n"
        f"Fuente: {source_name}\n"
        f"URL: {source_url}\n"
        f"Título: {page.get('title') or ''}\n"
        f"Texto de la página:\n{str(page.get('text') or '')[:1200]}"
    )
    payload = {
        "model": model,
        "temperature": 0.4,
        "max_tokens": 500,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
    }
    last_error = "DeepSeek no respondió"
    async with httpx.AsyncClient(timeout=45.0) as client:
        for _ in range(2):
            try:
                response = await client.post(f"{base}/chat/completions", headers=headers, json=payload)
            except httpx.HTTPError as exc:
                last_error = f"No se pudo conectar con DeepSeek: {exc}"
                continue
            if response.status_code >= 500:
                last_error = f"DeepSeek respondió HTTP {response.status_code}"
                continue
            if response.status_code >= 400:
                raise DeepSeekError(f"DeepSeek rechazó el texto: {response.text[:300]}")
            try:
                content = response.json()["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise DeepSeekError("DeepSeek devolvió un formato inesperado") from exc
            data = _extract_json(content or "")
            caption = _with_source(str(data.get("caption") or "").strip(), source_name, source_url)
            if len(caption) < 20:
                raise InstagramError("DeepSeek no escribió el mensaje del post")
            return caption
    raise DeepSeekError(last_error)


async def _draft_post() -> dict[str, str]:
    page = await _page_with_media()
    video = str(page.get("chosen_video") or "")
    image = str(page.get("chosen_image") or "")
    caption = await _write_caption(page)
    return {
        "caption": caption,
        "media_type": "video" if video else "image",
        "media_url": video or image,
        "source_url": str(page.get("url") or ""),
        "source_name": str(page.get("source_name") or page.get("title") or ""),
    }


async def _wait_container(creation_id: str, token: str) -> None:
    for _ in range(24):
        data = await _graph_get(creation_id if creation_id.startswith("/") else f"/{creation_id}", token, {"fields": "status_code"})
        code = str(data.get("status_code") or "")
        if code == "FINISHED":
            return
        if code in {"ERROR", "EXPIRED"}:
            raise InstagramError("Instagram no pudo preparar el video de esa página")
        await asyncio.sleep(5)
    raise InstagramError("Instagram sigue procesando el video. Intenta crear el post de nuevo.")


async def _publish(draft: dict[str, str]) -> dict[str, Any]:
    conn = _require_connection()
    ig_id = str(conn.get("ig_user_id") or "").strip()
    token = conn["page_access_token"]
    if not ig_id:
        raise InstagramError("Falta la cuenta de Instagram conectada")
    payload: dict[str, Any] = {"caption": draft["caption"]}
    if draft["media_type"] == "video":
        payload.update(
            {
                "media_type": "REELS",
                "video_url": draft["media_url"],
                "share_to_feed": "true",
            }
        )
    else:
        payload["image_url"] = draft["media_url"]
    created = await _graph_post(f"/{ig_id}/media", token, payload)
    creation_id = str(created.get("id") or "")
    if not creation_id:
        raise InstagramError("Instagram no creó el contenedor del post")
    if draft["media_type"] == "video":
        await _wait_container(creation_id, token)
    published = await _graph_post(f"/{ig_id}/media_publish", token, {"creation_id": creation_id})
    media_id = str(published.get("id") or "")
    permalink = ""
    if media_id:
        try:
            info = await _graph_get(f"/{media_id}", token, {"fields": "permalink"})
            permalink = str(info.get("permalink") or "")
        except InstagramError:
            permalink = ""
    return {
        "id": media_id,
        "permalink": permalink,
        "caption": draft["caption"],
        "media_type": draft["media_type"],
        "source_url": draft["source_url"],
        "source_name": draft["source_name"],
    }


async def create_instagram_post() -> dict[str, Any]:
    draft = await _draft_post()
    return await _publish(draft)
