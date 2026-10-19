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
_NEWS_QUERIES = (
    "noticias cultura conciertos Santiago",
    "agenda cultural Santiago esta semana",
)
_NEWS_HOSTS = (
    "latercera.com",
    "emol.com",
    "biobiochile.cl",
    "cooperativa.cl",
    "t13.cl",
    "24horas.cl",
    "cnnchile.com",
    "elmostrador.cl",
    "meganoticias.cl",
    "lacuarta.com",
    "adnradio.cl",
    "df.cl",
    "lun.com",
    "publimetro.cl",
    "chvnoticias.cl",
)
_BLOG_HOSTS = (
    "medium.com",
    "blogspot.com",
    "wordpress.com",
    "substack.com",
    "tumblr.com",
    "wixsite.com",
    "blogger.com",
)
_SITE_FILTER = (
    "(site:latercera.com OR site:emol.com OR site:biobiochile.cl "
    "OR site:cooperativa.cl OR site:t13.cl OR site:24horas.cl)"
)
_HOSTEL_SHOTS = (
    {
        "subject": "Desayuno",
        "image": "https://blackcathostal.com/cappa/img/restaurant/1.webp",
        "title": "Desayuno en Black Cat",
        "source_url": "https://blackcathostal.com/restaurant",
        "focus": (
            "La foto es el desayuno del hostal. Escribe solo del desayuno incluido y del horario "
            "que está en el material. No hables de la ciudad, de plazas ni de otra foto."
        ),
    },
    {
        "subject": "Habitación Queen",
        "image": "https://blackcathostal.com/cappa/img/rooms/blackcat/queen.webp",
        "title": "Habitación Queen",
        "source_url": "https://blackcathostal.com/rooms",
        "focus": (
            "La foto es la habitación con cama queen. Escribe solo de esa habitación: cama, baño "
            "y el precio del material si aparece. No hables del desayuno ni de un paisaje."
        ),
    },
    {
        "subject": "Habitación Twin",
        "image": "https://blackcathostal.com/cappa/img/rooms/blackcat/twin.webp",
        "title": "Habitación Twin",
        "source_url": "https://blackcathostal.com/rooms",
        "focus": (
            "La foto es la habitación con dos camas. Escribe solo de esa habitación y de su precio "
            "si está en el material."
        ),
    },
    {
        "subject": "Habitación familiar",
        "image": "https://blackcathostal.com/cappa/img/rooms/blackcat/family.webp",
        "title": "Habitación familiar",
        "source_url": "https://blackcathostal.com/rooms",
        "focus": (
            "La foto es la habitación familiar. Escribe solo de esa habitación, de cuántas personas "
            "caben y del precio si está en el material."
        ),
    },
    {
        "subject": "Patio",
        "image": "https://blackcathostal.com/cappa/img/about/patio.webp",
        "title": "Patio del hostal",
        "source_url": "https://blackcathostal.com",
        "focus": (
            "La foto es el patio del hostal. Escribe solo de ese espacio y de la dirección. "
            "No hables de museos ni de un plano de la ciudad."
        ),
    },
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
- El texto tiene que hablar de lo mismo que muestra la foto. Si la foto es un desayuno, no hables de la ciudad. Si la foto es un concierto, no hables de otra cosa.
- La fuente es un medio de noticias o la web del hostal. No cites Wikipedia ni un blog.
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


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _is_wikipedia(url: str) -> bool:
    host = _host(url)
    return "wikipedia.org" in host or "wikimedia.org" in host


def _is_verified_news(url: str) -> bool:
    host = _host(url)
    path = urlparse(url).path.lower()
    if not host or _is_wikipedia(url):
        return False
    if any(host == name or host.endswith(f".{name}") for name in _BLOG_HOSTS):
        return False
    if path == "/blog" or path.startswith("/blog/") or "/blog/" in path:
        return False
    return any(host == name or host.endswith(f".{name}") for name in _NEWS_HOSTS)


def _tokens(value: str) -> set[str]:
    text = re.sub(r"[^a-záéíóúüñ0-9 ]", " ", str(value or "").lower())
    skip = {
        "santiago", "chile", "para", "como", "desde", "cerca", "horario", "horarios",
        "esta", "semana", "foto", "file", "entradas", "llegar",
    }
    return {word for word in text.split() if len(word) > 3 and word not in skip}


def _plain_line(value: str) -> str:
    keep_short = {"de", "del", "el", "la", "las", "los", "en", "y", "a"}
    words = [
        word
        for word in re.sub(r"[^A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9 ]", " ", value).split()
        if len(word) > 2 or word.lower() in keep_short
    ]
    line = ""
    for word in words[:6]:
        trial = f"{line} {word}".strip()
        if len(trial) > 36:
            break
        line = trial
    return line


def _mentions(subject: str, *parts: str) -> bool:
    wanted = _tokens(subject)
    if not wanted:
        return True
    blob = set(_tokens(" ".join(parts)))
    for word in list(blob):
        if len(word) > 4 and word.endswith("s"):
            blob.add(word[:-1])
        elif len(word) > 4:
            blob.add(f"{word}s")
    return bool(wanted & blob)


def _article_photo(media: dict[str, Any], subject: str) -> str:
    images = [item for item in media.get("images") or [] if isinstance(item, dict)]
    images.sort(key=lambda item: 0 if str(item.get("is_meta")) == "True" else 1)
    for item in images:
        url = str(item.get("url") or "")
        alt = str(item.get("alt") or "")
        if not url or _is_wikipedia(url):
            continue
        if str(item.get("is_meta")) == "True" or _mentions(subject, alt, url):
            return url
    return ""


def _next_hostel_shot() -> dict[str, str]:
    path = settings.uploads_dir / "cache" / "instagram_post_hostel.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    current = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    subjects = [item["subject"] for item in _HOSTEL_SHOTS]
    try:
        index = subjects.index(current)
    except ValueError:
        index = -1
    chosen = _HOSTEL_SHOTS[(index + 1) % len(_HOSTEL_SHOTS)]
    path.write_text(chosen["subject"], encoding="utf-8")
    return chosen


async def _interesting_page(queries: tuple[str, ...], subject: str) -> dict[str, Any]:
    seen: set[str] = set()
    for query in queries:
        found = await call_reply_tool(
            "search_web",
            {"query": f"{query} {_SITE_FILTER}", "max_results": 5},
        )
        for row in (found.get("results") or [])[:5]:
            url = str(row.get("url") or "").strip()
            title = str(row.get("title") or "")
            snippet = str(row.get("snippet") or "")
            needed = subject or query
            if not url or url in seen or not _is_verified_news(url):
                continue
            if not _mentions(needed, title, snippet):
                continue
            seen.add(url)
            media = await call_reply_tool("find_web_media", {"url": url})
            if not isinstance(media, dict) or not media.get("ok"):
                continue
            page_title = str(media.get("title") or title)
            page_text = str(media.get("text") or snippet)
            if not _mentions(needed, page_title, page_text, snippet):
                continue
            image = _article_photo(media, needed)
            videos = [
                str(item.get("url") or "")
                for item in media.get("videos") or []
                if item.get("url") and not _is_wikipedia(str(item.get("url") or ""))
            ]
            if not image and not videos:
                continue
            host = _host(str(media.get("url") or url))
            return {
                "url": str(media.get("url") or url),
                "source_name": host or str(media.get("source_name") or ""),
                "title": page_title,
                "text": page_text,
                "chosen_images": [image] if image else [],
                "chosen_video": videos[0] if videos else "",
                "photo_credit_name": "",
                "photo_credit_url": "",
                "plate": _plain_line(page_title) or _plain_line(subject),
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


def _story(
    theme: str,
    *,
    title: str,
    text: str,
    subject: str,
    focus: str,
    image: str = "",
    source_url: str = "https://blackcathostal.com",
    source_name: str = "Black Cat Hostal",
) -> dict[str, Any]:
    return {
        "theme": theme,
        "url": source_url,
        "source_name": source_name,
        "title": title,
        "text": text,
        "chosen_images": [image] if image else [],
        "chosen_video": "",
        "photo_credit_name": "",
        "photo_credit_url": "",
        "plate": subject,
        "focus": focus,
    }


async def _page_with_media() -> dict[str, Any]:
    """A news story or a hostel photo, and a caption about that same photo."""
    theme = _next_theme()
    if theme == "concierto":
        page = await _interesting_page(_CONCERT_QUERIES, "")
        if page:
            page["theme"] = theme
            page["focus"] = (
                "La foto es la de esa nota. Habla del mismo concierto, artista o sala. "
                "Si el material no trae el nombre, no lo inventes."
            )
            return page
        return _story(
            theme,
            title="Música en vivo",
            subject="Música en vivo",
            text="No salió una cartelera en un medio. Invita a mirar la agenda de conciertos en Santiago, sin inventar artista ni fecha.",
            focus="La imagen dice Música en vivo. Habla de música en vivo en Santiago, sin inventar artista, sala ni fecha.",
        )
    if theme == "juego":
        query, place = _next_outing()
        page = await _interesting_page((query,), place)
        if page:
            page["theme"] = theme
            page["focus"] = (
                f"La foto es de {place}. Escribe una adivinanza corta cuya respuesta sea ese lugar. "
                "La respuesta va en la última línea. No hables de otro lugar."
            )
            page["plate"] = _plain_line(place) or page.get("plate") or place
            return page
        return _story(
            theme,
            title=place,
            subject=place,
            text=f"No salió una nota de {place}. Escribe una adivinanza cuya respuesta sea {place}.",
            focus=f"La imagen muestra {place}. La adivinanza se responde con ese lugar, en la última línea.",
        )
    if theme == "hotel":
        shot = _next_hostel_shot()
        info = await call_reply_tool("get_hostel_info", {})
        rooms = await call_reply_tool("search_rooms", {"query": ""})
        return _story(
            theme,
            title=shot["title"],
            subject=shot["subject"],
            image=shot["image"],
            source_url=shot["source_url"],
            text=json.dumps({"hostal": info, "habitaciones": rooms}, ensure_ascii=False)[:1800],
            focus=shot["focus"],
        )
    if theme == "noticia":
        page = await _interesting_page(_NEWS_QUERIES, "")
        if page:
            page["theme"] = theme
            page["focus"] = "La foto es la de esa nota. El dato de la primera línea tiene que ser el de esa misma nota."
            return page
        return _story(
            theme,
            title="Agenda de Santiago",
            subject="Agenda cultural",
            text="No salió una nota de un medio. Invita a revisar la agenda cultural de Santiago, sin inventar una función.",
            focus="La imagen dice Agenda cultural. No inventes un artista ni una fecha.",
        )
    query, place = _next_outing()
    page = await _interesting_page((query,), place)
    if page:
        page["theme"] = "turismo"
        page["focus"] = (
            f"La foto es de {place}. Habla solo de ese lugar, con un dato que esté en el material. "
            "No cambies el lugar."
        )
        page["plate"] = _plain_line(place) or page.get("plate") or place
        return page
    return _story(
        "turismo",
        title=place,
        subject=place,
        text=f"No salió una nota de {place}. Invita a ese plan con un dato general, sin inventar horarios ni precios.",
        focus=f"La imagen muestra {place}. El texto habla solo de ese lugar.",
    )


async def _write_caption(page: dict[str, Any]) -> str:
    base, model, headers = _require_deepseek()
    source_url = str(page.get("url") or "").strip()
    source_name = str(page.get("source_name") or page.get("title") or "").strip()
    theme = str(page.get("theme") or "turismo")
    focus = str(page.get("focus") or "").strip() or {
        "hotel": "Un detalle real del hostal que coincida con la foto.",
        "turismo": "El mismo lugar que muestra la foto, con un dato práctico del material.",
        "juego": "Una adivinanza cuya respuesta sea el lugar de la foto. La respuesta va en la última línea.",
        "concierto": "El mismo concierto de la nota. Si no hay artista o fecha, no los inventes.",
        "noticia": "El dato de esa nota en la primera línea.",
    }.get(theme, "Lo mismo que muestra la foto.")
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


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    pairs = (
        ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/segoeui.ttf"),
        ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"),
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ),
        (
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ),
    )
    for bold_path, regular_path in pairs:
        path = Path(bold_path if bold else regular_path)
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines[:2]


def _with_plate(image: Image.Image, title: str) -> Image.Image:
    """Bottom caption bar: kicker, gold rule, title. Not loose type on the sky."""
    line = _plain_line(title)
    if not line:
        return image
    width, height = image.size
    framed = image.convert("RGBA")
    plate = Image.new("RGBA", framed.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(plate)
    title_font = _font(max(32, width // 18), bold=True)
    kicker_font = _font(max(14, width // 46))
    lines = _wrap(draw, line, title_font, int(width * 0.82))
    panel_h = max(int(height * 0.18), int(title_font.size * (len(lines) + 1.6)))
    top = height - panel_h
    draw.rectangle((0, top, width, height), fill=(14, 14, 14, 228))
    draw.rectangle((0, top, width, top + max(3, width // 280)), fill=(212, 175, 110, 255))
    margin = int(width * 0.07)
    draw.text((margin, top + int(panel_h * 0.14)), "SANTIAGO", font=kicker_font, fill=(212, 175, 110, 255))
    y = top + int(panel_h * 0.40)
    for row in lines:
        draw.text((margin, y), row, font=title_font, fill=(255, 255, 255, 255))
        y += int(title_font.size * 1.15)
    return Image.alpha_composite(framed, plate).convert("RGB")


def _store_jpeg(jpeg: bytes) -> str:
    folder = settings.uploads_dir / "instagram-posts"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{uuid4().hex}.jpg"
    (folder / name).write_bytes(jpeg)
    return f"{_public_origin()}/api/instagram/media/{name}"


def _title_card(title: str) -> bytes:
    """Designed frame when the news photo could not be downloaded."""
    width, height = 1080, 1350
    image = Image.new("RGB", (width, height), (18, 18, 18))
    draw = ImageDraw.Draw(image)
    draw.rectangle((72, 72, width - 72, height - 72), outline=(212, 175, 110), width=2)
    kicker = _font(28)
    draw.text((120, 160), "SANTIAGO", font=kicker, fill=(212, 175, 110))
    draw.rectangle((120, 220, 280, 224), fill=(212, 175, 110))
    headline = _plain_line(title) or "Santiago"
    title_font = _font(78, bold=True)
    y = 320
    for row in _wrap(draw, headline, title_font, width - 240):
        draw.text((120, y), row, font=title_font, fill=(255, 255, 255))
        y += 96
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
        image = _with_plate(image, headline)
    return _jpeg_bytes(image)


async def _hosted_jpeg(image_urls: list[str], plate: str = "") -> str:
    for url in image_urls:
        if _is_wikipedia(url):
            continue
        try:
            content, _kind = await download_image(url, timeout_seconds=20, max_bytes=8_000_000)
            jpeg = _to_instagram_jpeg(content, plate)
        except (SourceFetchError, InstagramError, OSError, ValueError, httpx.HTTPError):
            continue
        return _store_jpeg(jpeg)
    return _store_jpeg(_title_card(plate or "Santiago"))


async def _draft_post() -> dict[str, Any]:
    page = await _page_with_media()
    images = [str(url) for url in page.get("chosen_images") or [] if url]
    video = str(page.get("chosen_video") or "")
    caption = await _write_caption(page)
    plate = str(page.get("plate") or page.get("title") or "Santiago")
    hosted = await _hosted_jpeg(images, plate)
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
