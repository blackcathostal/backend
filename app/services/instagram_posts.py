"""Create one Instagram feed post from a web page, using MCP tools."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from PIL import Image

from app.core.config import settings
from app.services.ai_source_fetcher import SourceFetchError, download_image
from app.services.deepseek_instagram import (
    DeepSeekError,
    _extract_json,
    _require_deepseek,
)
from app.services.google_places_client import places_headers
from app.services.hostel_directions import directions_to_hostel
from app.services.instagram import (
    InstagramError,
    _graph_get,
    _graph_post,
    _require_connection,
)
from app.services.mcp_sources import call_reply_tool
from app.services.short_links import shorten_url

_THEMES = ("concierto", "turismo", "llegada", "juego", "hotel", "noticia")
_CONCERT_QUERIES = (
    "conciertos Santiago esta semana",
    "agenda conciertos Santiago",
    "recitales Santiago",
)
_CONCERT_HOSTS = (
    "puntoticket.com",
    "ticketplus.cl",
    "ticketmaster.cl",
    "ticketmaster.com",
    "latercera.com",
    "emol.com",
    "biobiochile.cl",
    "cooperativa.cl",
)
_OUTINGS = (
    ("mirador cerro San Cristóbal Santiago", "Cerro San Cristóbal"),
    ("Templo Bahá'í Peñalolén", "Templo Bahá'í"),
    ("cerro Santa Lucía Santiago", "Cerro Santa Lucía"),
    ("Barrio Lastarria Santiago", "Barrio Lastarria"),
    ("calle Nueva York Santiago centro", "Calle Nueva York"),
    ("Parque Bicentenario Vitacura", "Parque Bicentenario"),
    ("teleférico cerro San Cristóbal", "Teleférico San Cristóbal"),
    ("Museo Nacional de Bellas Artes Santiago", "Museo Nacional de Bellas Artes"),
    ("Barrio Italia Santiago", "Barrio Italia"),
    ("Zoológico Nacional Santiago", "Zoológico Nacional"),
    ("Termas de Colina", "Termas de Colina"),
    ("Parque Aguas de Ramón Santiago", "Parque Aguas de Ramón"),
    ("Barrio Bellavista Santiago", "Barrio Bellavista"),
    ("cerro Santa Lucía castillo", "Castillo Hidalgo"),
)
_ARRIVALS = (
    ("Aeropuerto de Santiago", "transit"),
    ("Terminal de Buses Alameda", "transit"),
    ("Estación Central Santiago", "transit"),
    ("Metro Santa Ana", "walking"),
    ("Metro República", "walking"),
    ("Metro Los Héroes", "walking"),
    ("Metro Cumming", "walking"),
    ("Plaza de Armas Santiago", "walking"),
    ("Costanera Center", "transit"),
    ("Barrio Italia Santiago", "transit"),
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
_HASHTAGS = {
    "concierto": ("#Santiago", "#Conciertos"),
    "turismo": ("#Santiago", "#Chile"),
    "juego": ("#Santiago", "#Chile"),
    "hotel": ("#BlackCatHostal", "#Santiago", "#BarrioBrasil"),
    "noticia": ("#Santiago", "#Chile"),
    "llegada": ("#BlackCatHostal", "#Santiago", "#BarrioBrasil"),
}
_WALKS = (
    ("Plaza Brasil Santiago", "Plaza Brasil"),
    ("Barrio Yungay Santiago", "Barrio Yungay"),
    ("Barrio Concha y Toro Santiago", "Barrio Concha y Toro"),
    ("Parque Forestal Santiago", "Parque Forestal"),
    ("Paseo Phillips Santiago", "Paseo Phillips"),
)

_SYSTEM = """Eres el community manager de Black Cat Hostal Boutique, en Santiago.
Los posts hablan de toda Santiago y de las salidas en cualquier dirección: museos, zoológicos, parques naturales, termas de Colina, Jahuel, Cauquenes, Pirque o la cordillera, cerros, barrios y conciertos. También hay cómo llegar al hostal desde el aeropuerto, un terminal o una estación de metro, y posts de chiste o adivinanza. La nieve y los centros de ski solo si el material es de temporada. No te quedes solo en Barrio Brasil ni solo en el Cajón del Maipo.
Escribes posts de Instagram que la gente se queda a leer.

Cómo escriben las cuentas de turismo que funcionan, sin copiarlas:
- La primera línea es la foto: la vista, la hora o la calle. "La cordillera detrás del San Cristóbal, a última hora."
- La segunda es para ir: una dirección corta, un horario o los minutos a pie desde el hostal, solo si está en el material.
- La tercera invita en una línea.
- Frases cortas. Un dato, no un folleto. Nada de "descubre la magia" ni de "abre el enlace y mira".
- No inventes fechas, artistas ni precios.
- Escribe como una persona del hostal, en voz baja, sin sonar a anuncio ni a texto generado.
- El texto tiene que hablar de lo mismo que muestra la foto. Si la foto es un desayuno, no hables de la ciudad. Si la foto es un concierto, no hables de otra cosa.
- Los conciertos salen de Ticketmaster, Puntoticket, Ticketplus o un medio. El cómo llegar sale de Google Maps. No cites Wikipedia ni un blog.
- No escribas hashtags.
- Al final deja la página en una línea natural: "Lo vi en ..." y la URL. Si el material trae un enlace bit.ly, déjalo entero en su propia línea. No escribas api.blackcathostal.com ni un enlace largo de Google.
Responde solo JSON: {"caption":"..."}
"""


_LONG_MAPS = re.compile(
    r"https?://(?:www\.)?(?:google\.com/maps|maps\.google\.com)\S*",
    re.IGNORECASE,
)


def _with_source(caption: str, source_name: str, source_url: str) -> str:
    text = _LONG_MAPS.sub("", caption)
    text = re.sub(r"https?://api\.blackcathostal\.com\S*", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    link = source_url.strip()
    if link.startswith("https://bit.ly/") or "bitly.com" in link:
        if link not in text:
            text = f"{text}\n\nEl mapa:\n{link}" if source_name.strip().lower() == "google maps" else f"{text}\n\n{link}"
    elif link and "api.blackcathostal.com" not in link and link not in text:
        label = source_name.strip() or "esta página"
        text = f"{text}\n\nLo vi en {label}\n{link}"
    return text[:2200]


def _with_hashtags(caption: str, theme: str) -> str:
    text = re.sub(r"(?<!\w)#\w+", "", caption)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    tags = " ".join(_HASHTAGS.get(theme, ("#Santiago", "#Chile"))[:3])
    return f"{text}\n\n{tags}"[:2200]


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


def _allowed_host(url: str, hosts: tuple[str, ...]) -> bool:
    host = _host(url)
    path = urlparse(url).path.lower()
    if not host or _is_wikipedia(url):
        return False
    if any(host == name or host.endswith(f".{name}") for name in _BLOG_HOSTS):
        return False
    if path == "/blog" or path.startswith("/blog/") or "/blog/" in path:
        return False
    return any(host == name or host.endswith(f".{name}") for name in hosts)


def _site_filter(hosts: tuple[str, ...]) -> str:
    clause = " OR ".join(f"site:{host}" for host in hosts[:8])
    return f"({clause})"


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


def _photo_media_url(name: str, key: str) -> str:
    del key
    return f"https://places.googleapis.com/v1/{name}/media?maxWidthPx=1600"


def _travel_photo_urls(photos: list[Any], key: str) -> list[str]:
    """Tall travel frames first. Wide ones stay as backup and get cropped later."""
    ranked: list[tuple[float, str]] = []
    for index, photo in enumerate(photos[:8]):
        if not isinstance(photo, dict):
            continue
        name = str(photo.get("name") or "")
        if not name:
            continue
        width = int(photo.get("widthPx") or 0)
        height = int(photo.get("heightPx") or 0)
        ratio = (width / height) if width and height else 1.2
        penalty = abs(ratio - 0.8) + (0.15 if index == 0 else 0)
        ranked.append((penalty, name))
    ranked.sort()
    return [_photo_media_url(name, key) for _, name in ranked[:8]]


async def _interesting_page(
    queries: tuple[str, ...],
    subject: str,
    hosts: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    allowed = hosts or _NEWS_HOSTS
    seen: set[str] = set()
    for query in queries:
        found = await call_reply_tool(
            "search_web",
            {"query": f"{query} {_site_filter(allowed)}", "max_results": 5},
        )
        for row in (found.get("results") or [])[:5]:
            url = str(row.get("url") or "").strip()
            title = str(row.get("title") or "")
            snippet = str(row.get("snippet") or "")
            needed = subject or query
            if not url or url in seen or not _allowed_host(url, allowed):
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


def _cycle_key(filename: str, keys: list[str]) -> int:
    path = settings.uploads_dir / "cache" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    current = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    try:
        index = keys.index(current)
    except ValueError:
        index = -1
    chosen = (index + 1) % len(keys)
    path.write_text(keys[chosen], encoding="utf-8")
    return chosen


async def _google_place(query: str) -> dict[str, Any]:
    key = (settings.google_places_api_key or "").strip()
    text = str(query or "").strip()
    if not key or len(text) < 2:
        return {}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://places.googleapis.com/v1/places:searchText",
                headers=places_headers(
                    "places.displayName,places.formattedAddress,places.googleMapsUri,"
                    "places.regularOpeningHours,places.photos"
                ),
                json={
                    "textQuery": f"{text}, Santiago de Chile",
                    "languageCode": "es",
                    "regionCode": "CL",
                    "maxResultCount": 1,
                },
            )
            response.raise_for_status()
            places = response.json().get("places") or []
    except (httpx.HTTPError, ValueError):
        return {}
    if not places:
        return {}
    place = places[0]
    name = str((place.get("displayName") or {}).get("text") or text)
    photo_urls = _travel_photo_urls(list(place.get("photos") or []), key)
    hours = (place.get("regularOpeningHours") or {}).get("weekdayDescriptions") or []
    return {
        "name": name,
        "address": str(place.get("formattedAddress") or ""),
        "maps_url": shorten_url(str(place.get("googleMapsUri") or "")),
        "hours": " · ".join(str(item) for item in hours[:2]),
        "photo_url": photo_urls[0] if photo_urls else "",
        "photo_urls": photo_urls,
    }


def _place_material(place: dict[str, str]) -> str:
    lines = [place.get("name") or "", place.get("address") or "", place.get("hours") or ""]
    if place.get("maps_url"):
        lines.append(f"Google Maps: {place['maps_url']}")
    return "\n".join(line for line in lines if line)


async def _with_travel_photo(page: dict[str, Any], query: str) -> dict[str, Any]:
    """Use a tall photo of the place, not a catalog image from the website."""
    place = await _google_place(query)
    photos = [str(url) for url in place.get("photo_urls") or [] if url]
    if not photos and place.get("photo_url"):
        photos = [str(place["photo_url"])]
    if not photos:
        return page
    backups = [
        str(url)
        for url in page.get("chosen_images") or []
        if url and "blackcathostal.com" not in str(url) and "/cappa/" not in str(url)
    ]
    page["chosen_images"] = [*photos, *backups]
    page["subject"] = place.get("name") or page.get("subject") or query
    page["title"] = place.get("name") or page.get("title") or query
    extra = _place_material(place)
    if extra and extra not in str(page.get("text") or ""):
        page["text"] = f"{page.get('text') or ''}\n\n{extra}".strip()[:1800]
    if place.get("maps_url"):
        page["url"] = place["maps_url"]
        page["source_name"] = "Google Maps"
    return page


async def _with_google_photo(page: dict[str, Any], query: str) -> dict[str, Any]:
    if page.get("chosen_images") or page.get("chosen_video"):
        return page
    place = await _google_place(query)
    if not place:
        page["cover_detail"] = str(page.get("title") or query)
        return page
    page["text"] = f"{page.get('text') or ''}\n\n{_place_material(place)}".strip()[:1800]
    page["title"] = place.get("name") or page.get("title")
    page["cover_detail"] = place.get("address") or str(page.get("title") or query)
    if place.get("photo_url"):
        page["chosen_images"] = [place["photo_url"]]
    if place.get("maps_url") and str(page.get("url") or "").rstrip("/").endswith("blackcathostal.com"):
        page["url"] = place["maps_url"]
        page["source_name"] = "Google Maps"
    return page


async def _ticketmaster_concert() -> dict[str, Any]:
    key = (settings.ticketmaster_api_key or "").strip()
    if not key:
        return {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                settings.ticketmaster_events_api_url,
                params={
                    "apikey": key,
                    "city": "Santiago",
                    "countryCode": "CL",
                    "classificationName": "music",
                    "startDateTime": now,
                    "size": 20,
                    "sort": "date,asc",
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return {}
    shows = []
    for event in ((payload.get("_embedded") or {}).get("events") or []):
        if not isinstance(event, dict):
            continue
        name = str(event.get("name") or "").strip()
        start = (event.get("dates") or {}).get("start") or {}
        when = str(start.get("localDate") or start.get("dateTime") or "").strip()
        venue = (((event.get("_embedded") or {}).get("venues") or [{}])[0] or {})
        venue_name = str(venue.get("name") or "").strip()
        image = ""
        widest = 0
        for item in event.get("images") or []:
            url = str(item.get("url") or "")
            width = int(item.get("width") or 0)
            if url.startswith("https://") and width >= widest and "logo" not in url.lower():
                image = url
                widest = width
        if name and when:
            shows.append(
                {
                    "id": str(event.get("id") or name),
                    "name": name,
                    "when": when,
                    "venue": venue_name,
                    "url": str(event.get("url") or ""),
                    "image": image,
                }
            )
    if not shows:
        return {}
    index = _cycle_key("instagram_post_concert.txt", [item["id"] for item in shows])
    show = shows[index]
    text = f"{show['name']}\nFecha: {show['when']}\nLugar: {show['venue']}\n{show['url']}"
    page = _story(
        "concierto",
        title=show["name"],
        subject=show["name"],
        image=show["image"],
        source_url=show["url"] or "https://www.ticketmaster.cl",
        source_name="Ticketmaster",
        text=text,
        focus=(
            "La foto es de este concierto. Di el artista, la sala y la fecha solo si están en el material. "
            "No inventes otro show."
        ),
    )
    page["cover_detail"] = f"{show['when']} · {show['venue']}".strip(" ·")
    if not show["image"]:
        page = await _with_google_photo(page, show["venue"] or show["name"])
    return page


async def _arrival_page() -> dict[str, Any]:
    index = _cycle_key("instagram_post_arrival.txt", [item[0] for item in _ARRIVALS])
    origin, mode = _ARRIVALS[index]
    route = directions_to_hostel(origin, mode)
    place = await _google_place(origin)
    return _story(
        "llegada",
        title=f"Desde {origin}",
        subject=origin,
        image=str(place.get("photo_url") or ""),
        source_url=str(route.get("maps_url") or ""),
        source_name="Google Maps",
        text=str(route.get("summary") or ""),
        focus=(
            f"La foto es de {origin}. Explica cómo llegar desde ahí al hostal, solo con el tiempo, "
            "la distancia y las líneas del material. Si hay un enlace bit.ly, déjalo en su línea. "
            "No inventes un recorrido."
        ),
    )


async def _page_with_media() -> dict[str, Any]:
    """A real photo of the subject, or a designed card when no photo exists."""
    theme = _next_theme()
    if theme == "concierto":
        show = await _ticketmaster_concert()
        if show:
            return show
        page = await _interesting_page(_CONCERT_QUERIES, "", hosts=_CONCERT_HOSTS)
        if page:
            page["theme"] = theme
            page["focus"] = (
                "La foto es la de esa cartelera. Habla del mismo concierto, artista o sala. "
                "Si el material no trae el nombre, no lo inventes."
            )
            return page
        page = _story(
            theme,
            title="Conciertos en Santiago",
            subject="Conciertos en Santiago",
            text="No salió un show en Ticketmaster, Puntoticket ni en un medio. No inventes artista ni fecha.",
            focus="No hay un concierto confirmado. Invita a mirar Puntoticket o Ticketmaster, sin inventar un show.",
        )
        return await _with_travel_photo(page, "Movistar Arena Santiago")
    if theme == "llegada":
        return await _arrival_page()
    if theme == "juego":
        query, place = _next_outing()
        page = await _interesting_page((query,), place)
        if not page:
            page = _story(
                theme,
                title=place,
                subject=place,
                text=f"Escribe una adivinanza cuya respuesta sea {place}.",
                focus="",
            )
        page["theme"] = theme
        page["focus"] = (
            f"La foto es una postal de {place}. Escribe una adivinanza corta cuya respuesta sea ese lugar. "
            "La respuesta va en la última línea."
        )
        return await _with_travel_photo(page, place)
    if theme == "hotel":
        index = _cycle_key("instagram_post_walk.txt", [item[1] for item in _WALKS])
        query, place = _WALKS[index]
        info = await call_reply_tool("get_hostel_info", {})
        if not isinstance(info, dict):
            info = {}
        page = _story(
            theme,
            title=place,
            subject=place,
            source_url="https://blackcathostal.com",
            source_name="Black Cat Hostal",
            text=(
                f"Paseo: {place}. "
                f"Hostal en {info.get('address') or 'Compañía de Jesús 1921'}. "
                f"Desayuno: {info.get('breakfast_hours') or ''}. "
                f"Check-in: {info.get('check_in_time') or ''}."
            ),
            focus=(
                f"La foto es de {place}: calle, plaza o fachada, como una postal. "
                "Primera línea describe esa foto. Una sola línea puede decir que el hostal queda cerca, "
                "con un dato real. No describas una habitación de catálogo."
            ),
        )
        return await _with_travel_photo(page, query)
    if theme == "noticia":
        page = await _interesting_page(_NEWS_QUERIES, "")
        if page:
            page["theme"] = theme
            page["focus"] = "La foto es la de esa nota. El dato de la primera línea tiene que ser el de esa misma nota."
            return page
        page = _story(
            theme,
            title="Agenda de Santiago",
            subject="Agenda cultural",
            text="No salió una nota de un medio. Invita a revisar la agenda cultural de Santiago, sin inventar una función.",
            focus="No inventes un artista ni una fecha.",
        )
        return await _with_travel_photo(page, "centro cultural Santiago")
    query, place = _next_outing()
    page = await _interesting_page((query,), place)
    if not page:
        page = _story(
            "turismo",
            title=place,
            subject=place,
            text=f"Datos de {place}. No inventes horarios ni precios que no estén escritos.",
            focus="",
        )
    page["theme"] = "turismo"
    page["focus"] = (
        f"La foto es una postal de {place}: vista, calle o fachada. "
        "La primera línea describe esa foto. Después, un dato práctico que esté en el material. "
        "No suenes a folleto ni a catálogo."
    )
    return await _with_travel_photo(page, place)


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
            caption = _with_hashtags(
                _with_source(str(data.get("caption") or "").strip(), source_name, source_url),
                theme,
            )
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


def _store_jpeg(jpeg: bytes) -> str:
    folder = settings.uploads_dir / "instagram-posts"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{uuid4().hex}.jpg"
    (folder / name).write_bytes(jpeg)
    return f"{_public_origin()}/api/instagram/media/{name}"


def _jpeg_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=88, optimize=True)
    return buffer.getvalue()


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/png,*/*;q=0.8",
}


def _looks_like_image(content: bytes, content_type: str) -> bool:
    if content.startswith((b"\xff\xd8\xff", b"\x89PNG")):
        return True
    if content[:4] == b"RIFF" and b"WEBP" in content[:16]:
        return True
    return content_type.startswith("image/") and "svg" not in content_type


def _place_photo_resource(url: str) -> str:
    marker = "places.googleapis.com/v1/"
    if marker not in url or "/media" not in url:
        return ""
    path = url.split(marker, 1)[1]
    return path.split("/media", 1)[0].split("?", 1)[0]


def _image_bytes(response: httpx.Response) -> bytes:
    kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    content = response.content
    if response.status_code != 200 or kind.startswith("text/") or kind in {"application/json", "text/html"}:
        raise InstagramError(f"La foto respondió HTTP {response.status_code}")
    if len(content) < 5000 or len(content) > 12_000_000 or not _looks_like_image(content, kind):
        raise InstagramError(f"La foto respondió HTTP {response.status_code} sin imagen")
    return content


async def _fetch_photo_bytes(url: str) -> bytes:
    if not str(url).startswith("https://"):
        raise InstagramError("La foto no es una dirección pública")
    key = (settings.google_places_api_key or "").strip()
    resource = _place_photo_resource(url)
    async with httpx.AsyncClient(timeout=40.0, follow_redirects=True) as client:
        if resource and key:
            media = f"https://places.googleapis.com/v1/{resource}/media"
            headers = {"X-Goog-Api-Key": key}
            response = await client.get(
                media,
                params={"maxWidthPx": 1600, "key": key},
                headers=headers,
            )
            try:
                return _image_bytes(response)
            except InstagramError:
                alt = await client.get(
                    media,
                    params={"maxWidthPx": 1600, "skipHttpRedirect": "true", "key": key},
                    headers=headers,
                )
                photo_uri = ""
                if alt.status_code == 200:
                    try:
                        photo_uri = str(alt.json().get("photoUri") or "")
                    except ValueError:
                        photo_uri = ""
                if not photo_uri.startswith("https://"):
                    raise InstagramError(f"La foto de Google respondió HTTP {response.status_code}")
                response = await client.get(photo_uri)
        else:
            response = await client.get(url, headers=_BROWSER_HEADERS)
        return _image_bytes(response)


def _to_instagram_jpeg(content: bytes) -> bytes:
    """Vertical 4:5, the frame tourism posts use in the feed."""
    image = Image.open(BytesIO(content)).convert("RGB")
    width, height = image.size
    if width < 180 or height < 180:
        raise InstagramError("La foto es demasiado pequeña")
    target = 4 / 5
    ratio = width / height
    if ratio > target:
        new_width = max(1, int(height * target))
        left = max(0, (width - new_width) // 2)
        image = image.crop((left, 0, min(width, left + new_width), height))
    elif ratio < target:
        new_height = max(1, int(width / target))
        top = max(0, int((height - new_height) * 0.28))
        image = image.crop((0, top, width, min(height, top + new_height)))
    image = image.resize((1080, 1350), Image.Resampling.LANCZOS)
    return _jpeg_bytes(image)


async def _hosted_jpeg(image_urls: list[str], failures: list[str] | None = None) -> str:
    for url in image_urls:
        if (
            not url
            or _is_wikipedia(url)
            or "api.blackcathostal.com/api/g/" in url
            or "/cappa/" in url
            or ("blackcathostal.com" in url and "places.googleapis.com" not in url)
        ):
            continue
        try:
            content = await _fetch_photo_bytes(url)
            jpeg = _to_instagram_jpeg(content)
        except (SourceFetchError, InstagramError, OSError, ValueError, httpx.HTTPError) as exc:
            if failures is not None and len(failures) < 3:
                reason = str(exc)
                if "AIza" not in reason and "key=" not in reason:
                    failures.append(reason[:120])
            continue
        return _store_jpeg(jpeg)
    return ""


async def _photo_for_page(
    page: dict[str, Any],
    failures: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    images = [str(url) for url in page.get("chosen_images") or [] if url]
    hosted = await _hosted_jpeg(images, failures)
    if hosted:
        return hosted, page
    queries = [
        str(page.get("subject") or ""),
        str(page.get("title") or ""),
        "Cerro San Cristóbal",
        "Barrio Lastarria",
    ]
    seen: set[str] = set()
    for query in queries:
        text = query.strip()
        if len(text) < 2 or text in seen:
            continue
        seen.add(text)
        place = await _google_place(text)
        if not place.get("photo_urls") and failures is not None and len(failures) < 3:
            failures.append(f"Google no devolvió fotos de {text[:60]}")
        urls = [str(url) for url in place.get("photo_urls") or [] if url]
        if place.get("photo_url"):
            urls.append(str(place["photo_url"]))
        hosted = await _hosted_jpeg(urls, failures)
        if not hosted:
            continue
        if text != str(page.get("subject") or ""):
            page = {
                **page,
                "subject": place.get("name") or text,
                "title": place.get("name") or text,
                "text": _place_material(place) or page.get("text") or "",
                "focus": f"La foto es de {place.get('name') or text}. Describe esa vista en la primera línea.",
            }
        return hosted, page
    return "", page


async def _draft_post() -> dict[str, Any]:
    page = await _page_with_media()
    video = str(page.get("chosen_video") or "")
    failures: list[str] = []
    hosted, page = await _photo_for_page(page, failures)
    if not hosted and not video:
        detail = failures[-1] if failures else "Google no devolvió una imagen"
        raise InstagramError(f"No pude bajar una foto para el post. {detail}")
    caption = await _write_caption(page)
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
