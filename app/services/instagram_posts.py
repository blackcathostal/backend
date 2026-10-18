"""Create one Instagram feed post from a web page, using MCP tools."""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from PIL import Image

from app.core.config import settings
from app.services.ai_source_fetcher import SourceFetchError, download_image
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
                media["chosen_images"] = images
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


def _public_origin() -> str:
    redirect = str(settings.meta_redirect_uri or "").strip()
    parsed = urlparse(redirect)
    if parsed.scheme == "https" and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "https://api.blackcathostal.com"


def _to_instagram_jpeg(content: bytes) -> bytes:
    image = Image.open(BytesIO(content)).convert("RGB")
    width, height = image.size
    if width < 2 or height < 2:
        raise InstagramError("La foto es demasiado pequeña")
    ratio = width / height
    if ratio < 0.8:
        new_height = int(width / 0.8)
        top = max(0, (height - new_height) // 2)
        image = image.crop((0, top, width, top + new_height))
    elif ratio > 1.91:
        new_width = int(height * 1.91)
        left = max(0, (width - new_width) // 2)
        image = image.crop((left, 0, left + new_width, height))
    width, height = image.size
    short_side = min(width, height)
    if short_side < 320:
        scale = 320 / short_side
        image = image.resize((int(width * scale), int(height * scale)), Image.Resampling.LANCZOS)
    if max(image.size) > 1440:
        scale = 1440 / max(image.size)
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=88, optimize=True)
    return buffer.getvalue()


async def _hosted_jpeg(image_urls: list[str]) -> str:
    folder = settings.uploads_dir / "instagram-posts"
    folder.mkdir(parents=True, exist_ok=True)
    for url in image_urls:
        try:
            content, _kind = await download_image(url, timeout_seconds=20, max_bytes=8_000_000)
            jpeg = _to_instagram_jpeg(content)
        except (SourceFetchError, InstagramError, OSError, ValueError, httpx.HTTPError):
            continue
        name = f"{uuid4().hex}.jpg"
        (folder / name).write_bytes(jpeg)
        return f"{_public_origin()}/api/instagram/media/{name}"
    raise InstagramError("La foto de esa página no se pudo preparar para Instagram.")


async def _draft_post() -> dict[str, Any]:
    page = await _page_with_media()
    images = [str(url) for url in page.get("chosen_images") or [] if url]
    video = str(page.get("chosen_video") or "")
    caption = await _write_caption(page)
    hosted = ""
    if images:
        try:
            hosted = await _hosted_jpeg(images)
        except InstagramError:
            if not video:
                raise
    if not video and not hosted:
        raise InstagramError("La foto de esa página no se pudo preparar para Instagram.")
    return {
        "caption": caption,
        "media_type": "video" if video else "image",
        "media_url": video or hosted,
        "image_url": hosted,
        "source_url": str(page.get("url") or ""),
        "source_name": str(page.get("source_name") or page.get("title") or ""),
    }


def _media_not_ready(exc: InstagramError) -> bool:
    text = str(exc).lower()
    return "not available" in text or "media id" in text


async def _wait_container(creation_id: str, token: str) -> None:
    path = creation_id if creation_id.startswith("/") else f"/{creation_id}"
    for _ in range(20):
        data = await _graph_get(path, token, {"fields": "status_code,status"})
        code = str(data.get("status_code") or "")
        if code == "FINISHED":
            return
        if code in {"ERROR", "EXPIRED"}:
            detail = str(data.get("status") or "").strip()
            raise InstagramError(detail or "Instagram no pudo preparar el archivo")
        await asyncio.sleep(3)
    raise InstagramError("Instagram no terminó de preparar el archivo. Intenta de nuevo.")


async def _publish_when_ready(ig_id: str, token: str, creation_id: str) -> dict[str, Any]:
    await _wait_container(creation_id, token)
    last_error = "Instagram todavía no tenía lista la foto"
    for _ in range(4):
        try:
            return await _graph_post(f"/{ig_id}/media_publish", token, {"creation_id": creation_id})
        except InstagramError as exc:
            if not _media_not_ready(exc):
                raise
            last_error = str(exc)
            await asyncio.sleep(4)
    raise InstagramError(last_error)


def _container_payload(draft: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"caption": draft["caption"]}
    if draft.get("media_type") == "video":
        payload.update(
            {
                "media_type": "REELS",
                "video_url": draft["media_url"],
                "share_to_feed": "true",
            }
        )
    else:
        payload["image_url"] = draft.get("image_url") or draft["media_url"]
    return payload


async def _create_container(ig_id: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return await _graph_post(f"/{ig_id}/media", token, payload)
    except InstagramError as exc:
        if "instagram_content_publish" in str(exc).lower():
            raise InstagramError(
                "Hay que volver a conectar Meta para poder publicar. "
                "En Configuraciones, Meta, pulsa Desconectar y luego Conectar Meta, "
                "y acepta el permiso para publicar contenido en Instagram."
            ) from exc
        raise


async def _publish(draft: dict[str, Any]) -> dict[str, Any]:
    conn = _require_connection()
    ig_id = str(conn.get("ig_user_id") or "").strip()
    token = conn["page_access_token"]
    if not ig_id:
        raise InstagramError("Falta la cuenta de Instagram conectada")
    payload = _container_payload(draft)
    try:
        created = await _create_container(ig_id, token, payload)
    except InstagramError:
        image_url = str(draft.get("image_url") or "")
        if draft.get("media_type") != "video" or not image_url:
            raise
        draft = {**draft, "media_type": "image", "media_url": image_url}
        created = await _create_container(ig_id, token, _container_payload(draft))
    creation_id = str(created.get("id") or "")
    if not creation_id:
        raise InstagramError("Instagram no creó el contenedor del post")
    try:
        published = await _publish_when_ready(ig_id, token, creation_id)
    except InstagramError:
        image_url = str(draft.get("image_url") or "")
        if draft.get("media_type") != "video" or not image_url:
            raise
        draft = {**draft, "media_type": "image", "media_url": image_url}
        created = await _create_container(ig_id, token, _container_payload(draft))
        creation_id = str(created.get("id") or "")
        published = await _publish_when_ready(ig_id, token, creation_id)
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
