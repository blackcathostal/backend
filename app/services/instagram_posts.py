"""Create one Instagram feed post from a web page, using MCP tools."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.services.deepseek_instagram import (
    DeepSeekError,
    _extract_json,
    _parse_tool_args,
    _require_deepseek,
)
from app.services.instagram import (
    InstagramError,
    _graph_get,
    _graph_post,
    _require_connection,
)
from app.services.mcp_sources import call_reply_tool

POST_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Busca en la web una nota o un video de Santiago útil para un huésped.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_web_media",
            "description": (
                "Lee una URL devuelta por search_web y trae fotos, videos y la fuente. "
                "Obligatoria antes de armar el post."
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hostel_info",
            "description": "Lee la dirección y los datos del hostal para mencionarlos solo si hacen falta.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

_SYSTEM = """Eres el community manager de Black Cat Hostal Boutique, en Compañía de Jesús 1921, Barrio Brasil, Santiago.
Creas un solo post de Instagram.

Reglas:
- Usa siempre las tools del MCP. Primero search_web. Después find_web_media con una URL de esos resultados.
- Si esa página no trae foto ni video, busca otra página y vuelve a llamar find_web_media.
- El post es un video si find_web_media devolvió videos. Si no, es una foto con un mensaje.
- media_url tiene que ser exactamente una URL de images o videos de find_web_media. No inventes enlaces.
- El mensaje va en español, claro y corto, pensado para quien visita Santiago.
- Cita siempre de dónde sale: el source_name y la url de find_web_media, al final del texto.
- No copies el artículo entero. No inventes precios ni horarios que no haya devuelto una tool.
- Al terminar responde solo este JSON:
{"caption":"...","media_type":"image|video","media_url":"https://...","source_url":"https://...","source_name":"..."}
"""


def _remember(name: str, result: dict[str, Any], found: dict[str, Any]) -> None:
    if name == "search_web":
        for row in result.get("results") or []:
            url = str(row.get("url") or "").strip()
            if url:
                found["pages"].add(url)
    if name != "find_web_media" or not result.get("ok"):
        return
    page = str(result.get("url") or "").strip()
    if page:
        found["pages"].add(page)
        found["sources"][page] = str(result.get("source_name") or result.get("title") or "").strip()
    for row in result.get("images") or []:
        url = str(row.get("url") or "").strip()
        if url:
            found["images"].add(url)
    for row in result.get("videos") or []:
        url = str(row.get("url") or "").strip()
        if url:
            found["videos"].add(url)


def _with_source(caption: str, source_name: str, source_url: str) -> str:
    text = caption.strip()
    if source_url not in text:
        label = source_name.strip() or "Fuente"
        text = f"{text}\n\nFuente: {label}\n{source_url}"
    return text[:2200]


async def _draft_post() -> dict[str, str]:
    base, model, headers = _require_deepseek()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": (
                "Crea un post de Instagram para los huéspedes del hostal. "
                "Busca en la web algo actual de Santiago, del centro o de Barrio Brasil, "
                "y publica el video de esa página si existe. Si no hay video, publica una foto con el mensaje. "
                "Siempre di de qué página viene."
            ),
        },
    ]
    found: dict[str, Any] = {"pages": set(), "images": set(), "videos": set(), "sources": {}}
    used: set[str] = set()

    async with httpx.AsyncClient(timeout=90.0) as client:
        for step in range(8):
            tool_choice: Any = "auto"
            if step == 0:
                tool_choice = {"type": "function", "function": {"name": "search_web"}}
            response = await client.post(
                f"{base}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "temperature": 0.4,
                    "tools": POST_TOOLS,
                    "tool_choice": tool_choice,
                    "messages": messages,
                },
            )
            if response.status_code >= 400:
                raise DeepSeekError(
                    f"DeepSeek error HTTP {response.status_code}",
                    details=response.text[:800],
                )
            payload = response.json()
            try:
                message = payload["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise DeepSeekError("Formato inesperado de DeepSeek", details=payload) from exc

            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content"),
                        "tool_calls": tool_calls,
                    }
                )
                for call in tool_calls:
                    fn = call.get("function") or {}
                    name = str(fn.get("name") or "")
                    used.add(name)
                    result = await call_reply_tool(name, _parse_tool_args(fn.get("arguments")))
                    if isinstance(result, dict):
                        _remember(name, result, found)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id") or name,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                continue

            if "search_web" not in used or "find_web_media" not in used:
                messages.append({"role": "assistant", "content": message.get("content") or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": "Todavía no publicas. Llama search_web y después find_web_media. No respondas el JSON aún.",
                    }
                )
                continue

            data = _extract_json(message.get("content") or "")
            media_url = str(data.get("media_url") or "").strip()
            source_url = str(data.get("source_url") or "").strip()
            media_type = "video" if media_url in found["videos"] else "image"
            if media_url not in found["images"] and media_url not in found["videos"]:
                messages.append({"role": "assistant", "content": message.get("content") or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "media_url no salió de find_web_media. "
                            "Elige una URL exacta de images o videos. Si no hay ninguna, busca otra página."
                        ),
                    }
                )
                continue
            if source_url not in found["pages"]:
                source_url = next(
                    (page for page, _name in found["sources"].items()),
                    "",
                )
            if not source_url:
                raise InstagramError("DeepSeek no indicó la página de origen del post")
            source_name = str(data.get("source_name") or found["sources"].get(source_url) or "").strip()
            caption = _with_source(str(data.get("caption") or "").strip(), source_name, source_url)
            if len(caption) < 20:
                raise InstagramError("DeepSeek no escribió el mensaje del post")
            return {
                "caption": caption,
                "media_type": media_type,
                "media_url": media_url,
                "source_url": source_url,
                "source_name": source_name,
            }

    raise DeepSeekError("DeepSeek no armó el post con las tools del MCP")


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
