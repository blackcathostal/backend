"""Create one Instagram feed post from a web page, using MCP tools."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from PIL import Image, ImageDraw, ImageFont

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

_THEMES = ("concierto", "turismo", "juego", "hotel", "noticia")
_CONCERT_QUERIES = (
    "conciertos Santiago esta semana cartelera",
    "agenda conciertos Santiago puntoticket",
    "recitales Santiago Teatro Municipal",
    "conciertos GAM Santiago agenda",
)
_OUTINGS = (
    ("museos de Santiago horarios y entradas", "museo Santiago"),
    ("Museo de Bellas Artes y Museo Histórico Nacional Santiago", "museo Santiago"),
    ("Zoológico Nacional Santiago horarios", "Zoológico Nacional Santiago"),
    ("Buin Zoo cómo llegar desde Santiago", "Buin Zoo"),
    ("parques naturales cerca de Santiago Río Clarillo", "parque nacional Santiago"),
    ("Parque Aguas de Ramón y Quebrada de Macul", "parque natural Santiago"),
    ("termas cerca de Santiago horarios", "termas Santiago"),
    ("Termas de Colina cómo llegar", "Termas de Colina"),
    ("Termas de Jahuel Los Andes", "Termas de Jahuel"),
    ("Termas de Cauquenes desde Santiago", "Termas de Cauquenes"),
    ("Baños El Corazón Pirque", "Baños El Corazón"),
    ("Termas Valle de Colina Cajón del Maipo", "termas cordillera Santiago"),
    ("qué hacer en Santiago esta semana", "Santiago Chile"),
    ("Cerro San Cristóbal teleférico Santiago", "Cerro San Cristóbal"),
    ("barrio Italia y Bellavista planes", "Bellavista Santiago"),
)
_SNOW_OUTINGS = (
    ("centros de ski Valle Nevado Farellones abiertos", "Valle Nevado nieve"),
    ("El Colorado Lagunillas nieve Santiago", "ski cordillera Santiago"),
)
_JUEGO_QUERIES = (
    "chistes cortos de turismo y viajes",
    "adivinanzas fáciles de Chile con respuesta",
    "adivinanzas de lugares y animales",
)
_NEWS_QUERIES = (
    "noticias cultura conciertos Santiago",
    "agenda cultural Santiago esta semana",
)

_SYSTEM = """Eres el community manager de Black Cat Hostal Boutique, en Santiago.
Los posts hablan de toda Santiago y de las salidas en cualquier dirección: museos, zoológicos, parques naturales, termas de Colina, Jahuel, Cauquenes, Pirque o la cordillera, cerros, barrios y conciertos. También hay posts de chiste o adivinanza de turismo. La nieve y los centros de ski solo si el material es de temporada. No te quedes solo en Barrio Brasil ni solo en el Cajón del Maipo.
Escribes posts de Instagram que la gente se queda a leer.

Cómo se hace un post que engancha, según las cuentas que funcionan:
- La primera línea para el dedo: una pregunta, una fecha o una frase concreta. Nada de "descubre la magia".
- Después, dos o tres datos que solo están en el material: quién, dónde, cuándo, precio o cómo llegar.
- Frases cortas, una idea por línea. Se puede usar un emoji, no una fila.
- Cierra con una invitación clara.
- No copies ejemplos ajenos. No inventes fechas, artistas ni precios.
- Escribe como una persona del hostal, en voz baja, sin hashtags y sin sonar a anuncio ni a texto generado.
- Al final deja la página en una línea natural: "Lo vi en ..." y la URL.
Responde solo JSON: {"caption":"..."}
"""


def _with_source(caption: str, source_name: str, source_url: str) -> str:
    text = caption.strip()
    if source_url not in text:
        label = source_name.strip() or "esta página"
        text = f"{text}\n\nLo vi en {label}\n{source_url}"
    return text[:2200]


def _snow_in_season() -> bool:
    month = datetime.now(ZoneInfo("America/Santiago")).month
    return month in {6, 7, 8, 9, 10}


def _outing_pool() -> tuple[tuple[str, str], ...]:
    if _snow_in_season():
        return _OUTINGS + _SNOW_OUTINGS
    return _OUTINGS


def _next_outing() -> tuple[str, str]:
    path = settings.uploads_dir / "cache" / "instagram_post_outing.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    pool = _outing_pool()
    current = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    queries = [item[0] for item in pool]
    try:
        index = queries.index(current)
    except ValueError:
        index = -1
    chosen = pool[(index + 1) % len(pool)]
    path.write_text(chosen[0], encoding="utf-8")
    return chosen


def _next_theme() -> str:
    path = settings.uploads_dir / "cache" / "instagram_post_theme.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    current = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    try:
        index = _THEMES.index(current)
    except ValueError:
        index = -1
    theme = _THEMES[(index + 1) % len(_THEMES)]
    path.write_text(theme, encoding="utf-8")
    return theme


def _is_wikipedia(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return "wikipedia.org" in host or "wikimedia.org" in host


def _tokens(value: str) -> set[str]:
    text = re.sub(r"[^a-záéíóúüñ0-9 ]", " ", str(value or "").lower())
    skip = {
        "santiago", "chile", "para", "como", "desde", "cerca", "horario", "horarios",
        "esta", "semana", "foto", "file", "entradas", "llegar",
    }
    return {word for word in text.split() if len(word) > 3 and word not in skip}


def _plain_line(value: str) -> str:
    words = [word for word in re.sub(r"[^A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9 ]", " ", value).split() if len(word) > 2]
    line = " ".join(words[:4]).strip()
    return line[:28]


def _pick_photo(photos: list[dict[str, Any]], query: str) -> tuple[dict[str, Any], int]:
    wanted = _tokens(query)
    best: dict[str, Any] = {}
    score = -1
    for photo in photos:
        current = len(wanted & _tokens(str(photo.get("title") or "")))
        if current > score:
            best = photo
            score = current
    return best, max(score, 0)


async def _photo_for(query: str) -> dict[str, str]:
    found = await call_reply_tool("search_tourism_photo", {"query": query})
    photos = list(found.get("photos") or []) if isinstance(found, dict) else []
    photo, score = _pick_photo(photos, query)
    if score == 0:
        shorter = " ".join(list(_tokens(query))[:3])
        if shorter:
            second = await call_reply_tool("search_tourism_photo", {"query": shorter})
            again = list(second.get("photos") or []) if isinstance(second, dict) else []
            better, better_score = _pick_photo(again, query)
            if better_score > 0:
                photo, score = better, better_score
    image_url = str(photo.get("image_url") or "")
    if not image_url:
        return {}
    return {
        "image_url": image_url,
        "source_url": str(photo.get("source_url") or ""),
        "title": str(photo.get("title") or ""),
        "headline": "" if score else _plain_line(query),
    }


async def _interesting_page(queries: tuple[str, ...]) -> dict[str, Any]:
    seen: set[str] = set()
    for query in queries:
        found = await call_reply_tool("search_web", {"query": query, "max_results": 5})
        for row in (found.get("results") or [])[:5]:
            url = str(row.get("url") or "").strip()
            if not url or url in seen or _is_wikipedia(url):
                continue
            seen.add(url)
            media = await call_reply_tool("find_web_media", {"url": url})
            if not isinstance(media, dict) or not media.get("ok"):
                continue
            images = [str(item.get("url") or "") for item in media.get("images") or [] if item.get("url")]
            videos = [str(item.get("url") or "") for item in media.get("videos") or [] if item.get("url")]
            photo = await _photo_for(str(media.get("title") or query))
            if photo.get("image_url"):
                images = [photo["image_url"], *images]
            if not images and not videos:
                continue
            return {
                "url": str(media.get("url") or url),
                "source_name": str(media.get("source_name") or media.get("title") or ""),
                "title": str(media.get("title") or ""),
                "text": str(media.get("text") or row.get("snippet") or ""),
                "chosen_images": images,
                "chosen_video": videos[0] if videos else "",
                "photo_credit_name": photo.get("title") or "",
                "photo_credit_url": photo.get("source_url") or "",
                "headline": photo.get("headline") or "",
            }
    return {}


async def _style_notes() -> str:
    """How other accounts open a post. Used as rhythm, never copied."""
    found = await call_reply_tool(
        "search_web",
        {"query": "instagram caption hooks travel posts ejemplos gancho primera línea", "max_results": 3},
    )
    lines = []
    for row in (found.get("results") or [])[:3]:
        snippet = str(row.get("snippet") or "").strip()
        title = str(row.get("title") or "").strip()
        if snippet:
            lines.append(f"- {title}: {snippet[:220]}")
    if not lines:
        return "Gancho en la primera línea. Luego un dato concreto. Frases cortas. Una invitación."
    return "\n".join(lines)


async def _page_with_media() -> dict[str, Any]:
    """Concert listings, city plans, hostel facts or culture news, with a photo or a video."""
    theme = _next_theme()
    photo_query = "concierto en vivo Santiago"
    text = ""
    source_url = "https://blackcathostal.com"
    source_name = "Black Cat Hostal"
    title = "Black Cat Hostal"
    if theme == "concierto":
        page = await _interesting_page(_CONCERT_QUERIES)
        if page:
            page["theme"] = theme
            return page
        text = "No salió una cartelera. Cuenta que en Santiago hay música en vivo en distintas comunas y invita a revisar la fuente."
        title = "Conciertos en Santiago"
    elif theme == "juego":
        page = await _interesting_page(_JUEGO_QUERIES)
        photo_query = "Santiago Chile turistas plaza"
        if page:
            page["theme"] = theme
            if not page.get("chosen_images"):
                photo = await _photo_for(photo_query)
                if photo.get("image_url"):
                    page["chosen_images"] = [photo["image_url"]]
                    page["photo_credit_name"] = photo.get("title") or ""
                    page["photo_credit_url"] = photo.get("source_url") or ""
                    page["headline"] = photo.get("headline") or ""
            return page
        text = (
            "Escribe un chiste corto de turismo o una adivinanza cuya respuesta sea un lugar real de Santiago. "
            "La respuesta va al final. Tiene que ser amable."
        )
        title = "Chiste de turismo"
    elif theme == "hotel":
        info = await call_reply_tool("get_hostel_info", {})
        rooms = await call_reply_tool("search_rooms", {"query": ""})
        text = json.dumps({"hostal": info, "habitaciones": rooms}, ensure_ascii=False)[:1800]
        title = "Detalles del hostal"
        photo_query = "Santiago Chile ciudad"
    elif theme == "noticia":
        page = await _interesting_page(_NEWS_QUERIES)
        if page:
            page["theme"] = theme
            return page
        text = "No salió una noticia reciente. Cuenta un plan cultural en cualquier comuna de Santiago."
        title = "Agenda de Santiago"
        photo_query = "Santiago Chile concierto plaza"
    else:
        query, photo_query = _next_outing()
        page = await _interesting_page((query,))
        if page:
            page["theme"] = "turismo"
            return page
        text = "Invita a un plan concreto en Santiago o en una salida desde la ciudad, con un detalle útil."
        title = "Turismo en Santiago"
    photo = await _photo_for(photo_query)
    if not photo.get("image_url"):
        photo = await _photo_for("Santiago Chile")
    images = [photo["image_url"]] if photo.get("image_url") else []
    return {
        "theme": theme,
        "url": source_url,
        "source_name": source_name,
        "title": title,
        "text": text,
        "chosen_images": images,
        "chosen_video": "",
        "photo_credit_name": photo.get("title") or "",
        "photo_credit_url": photo.get("source_url") or "",
        "headline": photo.get("headline") or "",
    }


async def _write_caption(page: dict[str, Any]) -> str:
    base, model, headers = _require_deepseek()
    source_url = str(page.get("url") or "").strip()
    source_name = str(page.get("source_name") or page.get("title") or "").strip()
    theme = str(page.get("theme") or "turismo")
    focus = {
        "hotel": "Un detalle real del hostal que dé ganas de quedarse: dirección, desayuno, horario o una habitación con su precio.",
        "turismo": "Un plan en Santiago o una salida en cualquier dirección: museo, zoológico, parque, termas o nieve. Las termas no son solo el Cajón del Maipo. Solo habla de nieve si el material lo dice. Incluye lugar y un dato práctico.",
        "juego": "Un chiste corto de turismo o una adivinanza. Si es adivinanza, la respuesta va en la última línea, después de un espacio. Amable, sin burlarte de nadie. El lugar, si lo nombras, tiene que ser real.",
        "concierto": "Si el material trae artista, sala o fecha, ponlos. Si no están, no los inventes. La sala puede estar en cualquier comuna de Santiago.",
        "noticia": "El dato nuevo en la primera línea y por qué un huésped querría ir.",
    }.get(theme, "Un plan concreto en Santiago.")
    notes = await _style_notes()
    user = (
        f"Tema del post: {theme}. {focus}\n"
        f"Así abren otros posts. No los copies, copia solo el ritmo:\n{notes}\n\n"
        f"Fuente: {source_name}\n"
        f"URL: {source_url}\n"
        f"Título: {page.get('title') or ''}\n"
        f"Material:\n{str(page.get('text') or '')[:1400]}"
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
            credit_url = str(page.get("photo_credit_url") or "").strip()
            credit_name = str(page.get("photo_credit_name") or "").strip()
            if credit_url and credit_url not in caption:
                caption = f"{caption}\n\nLa foto es de {credit_name or 'quien la tomó'}\n{credit_url}"
            if len(caption) < 20:
                raise InstagramError("DeepSeek no escribió el mensaje del post")
            return caption[:2200]
    raise DeepSeekError(last_error)


def _public_origin() -> str:
    redirect = str(settings.meta_redirect_uri or "").strip()
    parsed = urlparse(redirect)
    if parsed.scheme == "https" and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "https://api.blackcathostal.com"


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for path in (
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    ):
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _with_headline(image: Image.Image, headline: str) -> Image.Image:
    line = _plain_line(headline)
    if not line:
        return image
    width, height = image.size
    band = max(1, int(height * 0.18))
    shade = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shade_draw = ImageDraw.Draw(shade)
    for row in range(band):
        alpha = int(90 * (1 - row / band))
        shade_draw.line([(0, row), (width, row)], fill=(0, 0, 0, alpha))
    framed = Image.alpha_composite(image.convert("RGBA"), shade).convert("RGB")
    draw = ImageDraw.Draw(framed)
    font = _font(max(26, width // 18))
    x = int(width * 0.06)
    y = int(height * 0.045)
    draw.text((x + 1, y + 1), line, font=font, fill=(0, 0, 0))
    draw.text((x, y), line, font=font, fill=(255, 255, 255))
    return framed


def _store_jpeg(jpeg: bytes) -> str:
    folder = settings.uploads_dir / "instagram-posts"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{uuid4().hex}.jpg"
    (folder / name).write_bytes(jpeg)
    return f"{_public_origin()}/api/instagram/media/{name}"


def _made_scene(headline: str) -> bytes:
    """A real-looking frame when no usable photo came back. Not a poster."""
    width, height = 1080, 1350
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for row in range(height):
        blend = row / height
        color = (
            int(168 + 55 * blend),
            int(188 + 28 * blend),
            int(196 - 18 * blend),
        )
        draw.line([(0, row), (width, row)], fill=color)
    draw.polygon([(0, 860), (180, 740), (360, 900), (520, 690), (760, 930), (980, 720), (1080, 880), (1080, 1350), (0, 1350)], fill=(78, 96, 86))
    draw.polygon([(0, 1040), (240, 900), (480, 1080), (720, 860), (1080, 1020), (1080, 1350), (0, 1350)], fill=(58, 72, 64))
    grain = Image.effect_noise((width, height), 18).convert("L")
    image = Image.blend(image, Image.merge("RGB", (grain, grain, grain)), 0.07)
    image = _with_headline(image, headline or "Santiago")
    return _jpeg_bytes(image)


def _jpeg_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=88, optimize=True)
    return buffer.getvalue()


def _to_instagram_jpeg(content: bytes, headline: str = "") -> bytes:
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
    if headline:
        image = _with_headline(image, headline)
    return _jpeg_bytes(image)


async def _hosted_jpeg(image_urls: list[str], headline: str = "") -> str:
    for url in image_urls:
        try:
            content, _kind = await download_image(url, timeout_seconds=20, max_bytes=8_000_000)
            jpeg = _to_instagram_jpeg(content, headline)
        except (SourceFetchError, InstagramError, OSError, ValueError, httpx.HTTPError):
            continue
        return _store_jpeg(jpeg)
    return _store_jpeg(_made_scene(headline or "Santiago"))


async def _draft_post() -> dict[str, Any]:
    page = await _page_with_media()
    images = [str(url) for url in page.get("chosen_images") or [] if url]
    video = str(page.get("chosen_video") or "")
    caption = await _write_caption(page)
    headline = str(page.get("headline") or page.get("title") or "Santiago")
    hosted = await _hosted_jpeg(images, headline)
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
