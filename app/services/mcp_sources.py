from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import jwt
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.ai_sources import AiSources
from app.models.ai_generation_runs import AiGenerationRuns
from app.models.users import Users
from app.services.ai_api_sources import collect_external_api_materials
from app.services.hostel_directions import directions_from_hostel
from app.services.instagram_knowledge import hostel_info_dict, search_common_answers, search_rooms
from app.services.ai_source_fetcher import SourceFetchError, fetch_source_content

mcp = FastMCP(
    "Black Cat Tourism Sources",
    instructions=(
        "Responde con datos del hostal, las habitaciones, las preguntas frecuentes "
        "y, si falta algo, con búsqueda en la web. No inventes datos."
    ),
    streamable_http_path="/",
)


@mcp.tool(
    name="list_active_sources",
    description="Lista las fuentes HTTPS activas ordenadas por prioridad.",
    structured_output=True,
)
def list_active_sources(limit: int = 6) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit), settings.deepseek_max_sources))
    with SessionLocal() as db:
        rows = db.query(AiSources).filter(AiSources.is_active.is_(True)).all()
        recent_runs = (
            db.query(AiGenerationRuns.source_ids)
            .filter(
                AiGenerationRuns.status == "success",
                AiGenerationRuns.source_ids != "",
            )
            .order_by(AiGenerationRuns.created_at.desc(), AiGenerationRuns.id.desc())
            .limit(20)
            .all()
        )
        usage_count: Counter[int] = Counter()
        for (source_ids,) in recent_runs:
            for source_id in str(source_ids or "").split(","):
                if source_id.strip().isdigit():
                    usage_count[int(source_id)] += 1
        rows.sort(key=lambda row: (-row.priority, usage_count[row.id], row.id))
        rows = rows[:safe_limit]
        return {
            "sources": [
                {
                    "id": row.id,
                    "name": row.name,
                    "url": row.url,
                    "category": row.category,
                    "language": row.language,
                }
                for row in rows
            ]
        }


@mcp.tool(
    name="search_google_places",
    description=(
        "Busca lugares reales en Google Maps para enriquecer una guía turística de Santiago. "
        "Devuelve solo nombres, direcciones y horarios entregados por Google."
    ),
    structured_output=True,
)
async def search_google_places(query: str, max_results: int = 5) -> dict[str, Any]:
    if not settings.google_places_api_key.strip():
        return {"ok": False, "query": query, "error": "GOOGLE_PLACES_API_KEY no está configurada."}

    text_query = str(query or "").strip()[:180]
    if not text_query:
        return {"ok": False, "query": query, "error": "La consulta de Google Maps está vacía."}

    field_mask = ",".join(
        (
            "places.displayName",
            "places.formattedAddress",
            "places.googleMapsUri",
            "places.types",
            "places.rating",
            "places.regularOpeningHours",
        )
    )
    try:
        async with httpx.AsyncClient(timeout=settings.deepseek_source_timeout_seconds) as client:
            response = await client.post(
                "https://places.googleapis.com/v1/places:searchText",
                headers={
                    "X-Goog-Api-Key": settings.google_places_api_key,
                    "X-Goog-FieldMask": field_mask,
                    "Content-Type": "application/json",
                },
                json={
                    "textQuery": f"{text_query}, Santiago de Chile",
                    "languageCode": "es",
                    "regionCode": "CL",
                    "maxResultCount": max(1, min(int(max_results), 10)),
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "query": text_query, "error": f"No se pudo consultar Google Maps: {exc}"}

    places = []
    for place in payload.get("places", []):
        name = str(place.get("displayName", {}).get("text") or "").strip()
        address = str(place.get("formattedAddress") or "").strip()
        if not name or not address:
            continue
        hours = place.get("regularOpeningHours") or {}
        places.append(
            {
                "name": name,
                "address": address,
                "maps_url": place.get("googleMapsUri") or "",
                "types": place.get("types") or [],
                "rating": place.get("rating"),
                "opening_hours": hours.get("weekdayDescriptions") or [],
            }
        )
    return {"ok": True, "query": text_query, "places": places}


@mcp.tool(
    name="directions_from_hostel",
    description=(
        "Ruta a pie desde Black Cat Hostal, Compañía de Jesús 1921, hasta un lugar de Santiago. "
        "Úsala cuando pregunten cómo llegar a La Moneda, un museo o una estación."
    ),
    structured_output=True,
)
def directions_from_hostel_tool(destination: str) -> dict[str, Any]:
    return directions_from_hostel(destination)


def _faq_payload(query: str = "") -> dict[str, Any]:
    with SessionLocal() as db:
        items = search_common_answers(db, query)
    return {
        "ok": True,
        "query": str(query or "").strip(),
        "questions": [
            {"question": item.get("question") or "", "answer": item.get("answer") or ""}
            for item in items
        ],
    }


@mcp.tool(
    name="search_frequent_questions",
    description=(
        "Lee las preguntas frecuentes de Black Cat Hostal guardadas en configuraciones, "
        "cada una con su respuesta. Úsala antes de contestar una duda del huésped."
    ),
    structured_output=True,
)
def search_frequent_questions(query: str = "") -> dict[str, Any]:
    return _faq_payload(query)


@mcp.tool(
    name="search_common_answers",
    description=(
        "Lee las preguntas frecuentes guardadas en configuraciones. "
        "Úsala primero. Si la respuesta no está completa, busca también en la web."
    ),
    structured_output=True,
)
def search_common_answers_tool(query: str = "") -> dict[str, Any]:
    return _faq_payload(query)


@mcp.tool(
    name="search_rooms",
    description=(
        "Lee nombre, tipo, capacidad y precio actual de las habitaciones del panel. "
        "Úsala siempre que pregunten precios o qué incluye una habitación."
    ),
    structured_output=True,
)
def search_rooms_tool(query: str = "") -> dict[str, Any]:
    with SessionLocal() as db:
        rooms = search_rooms(db, query)
    return {"ok": True, "query": str(query or "").strip(), "rooms": rooms}


@mcp.tool(
    name="get_hostel_info",
    description=(
        "Lee check-in, check-out, desayuno, estacionamiento, WhatsApp, correo y dirección "
        "guardados en la información del hostal."
    ),
    structured_output=True,
)
def get_hostel_info_tool() -> dict[str, Any]:
    with SessionLocal() as db:
        info = hostel_info_dict(db)
    return {"ok": True, **info}


@mcp.tool(
    name="get_directions_from_hostel",
    description=(
        "Ruta a pie desde Black Cat Hostal, Compañía de Jesús 1921, hasta un lugar de Santiago."
    ),
    structured_output=True,
)
def get_directions_from_hostel_tool(destination: str) -> dict[str, Any]:
    return directions_from_hostel(destination)


def _web_result_url(raw_href: str) -> str:
    href = unquote(str(raw_href or "").strip())
    if href.startswith("//"):
        href = f"https:{href}"
    parsed = urlparse(href)
    if "duckduckgo.com" in (parsed.hostname or "") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        href = unquote(target)
    if href.startswith("https://"):
        return href
    return ""


def _strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", text).strip()


def _parse_duckduckgo(html: str, limit: int) -> list[dict[str, str]]:
    blocks = re.findall(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>(?:.*?)class="result__snippet"[^>]*>(.*?)</(?:a|td|span)>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for href, title_html, snippet_html in blocks:
        url = _web_result_url(href)
        title = _strip_tags(title_html)
        snippet = _strip_tags(snippet_html)
        if not url or not title or url in seen:
            continue
        seen.add(url)
        results.append({"title": title[:180], "url": url, "snippet": snippet[:320]})
        if len(results) >= limit:
            break
    return results


async def _wikipedia_search(query: str, limit: int) -> list[dict[str, str]]:
    try:
        async with httpx.AsyncClient(timeout=settings.deepseek_source_timeout_seconds) as client:
            response = await client.get(
                "https://es.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": limit,
                    "format": "json",
                    "utf8": 1,
                },
                headers={"User-Agent": "BlackCatTourismBot/1.0 (+https://blackcathostal.com)"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    results: list[dict[str, str]] = []
    for item in (payload.get("query") or {}).get("search") or []:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        slug = title.replace(" ", "_")
        results.append(
            {
                "title": title,
                "url": f"https://es.wikipedia.org/wiki/{slug}",
                "snippet": _strip_tags(str(item.get("snippet") or ""))[:320],
            }
        )
    return results


@mcp.tool(
    name="search_web",
    description=(
        "Busca en la web cuando la respuesta no está en las preguntas frecuentes, "
        "en las habitaciones ni en la información del hostal. "
        "Sirve para lugares de Santiago, horarios públicos y datos que no están en el panel."
    ),
    structured_output=True,
)
async def search_web(query: str, max_results: int = 5) -> dict[str, Any]:
    text = str(query or "").strip()[:180]
    if not text:
        return {"ok": False, "query": text, "error": "La búsqueda está vacía."}
    limit = max(1, min(int(max_results or 5), 5))
    results: list[dict[str, str]] = []
    error = ""
    try:
        async with httpx.AsyncClient(
            timeout=settings.deepseek_source_timeout_seconds,
            follow_redirects=True,
        ) as client:
            response = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": text},
                headers={
                    "User-Agent": "BlackCatTourismBot/1.0 (+https://blackcathostal.com)",
                    "Accept": "text/html",
                },
            )
            response.raise_for_status()
            results = _parse_duckduckgo(response.text, limit)
    except (httpx.HTTPError, ValueError) as exc:
        error = str(exc)
    if not results:
        results = await _wikipedia_search(text, limit)
    if not results:
        return {"ok": False, "query": text, "results": [], "error": error or "Sin resultados web."}
    return {"ok": True, "query": text, "results": results}


@mcp.tool(
    name="read_web_page",
    description=(
        "Lee el texto de una página HTTPS devuelta por search_web. "
        "No uses direcciones que no hayan salido de una tool."
    ),
    structured_output=True,
)
async def read_web_page(url: str) -> dict[str, Any]:
    try:
        material = await fetch_source_content(
            str(url or "").strip(),
            timeout_seconds=settings.deepseek_source_timeout_seconds,
            max_bytes=settings.deepseek_source_max_bytes,
        )
    except (SourceFetchError, Exception) as exc:
        return {"ok": False, "url": url, "error": str(exc)}
    return {
        "ok": True,
        "url": material.get("url") or url,
        "title": material.get("title") or "",
        "text": str(material.get("text") or "")[:1800],
    }


_SKIPPED_MEDIA_HOSTS = (
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "twitter.com",
    "x.com",
)


def _direct_file(url: str, extensions: tuple[str, ...]) -> bool:
    path = urlparse(str(url or "")).path.lower()
    return path.endswith(extensions)


def _publishable_host(url: str) -> bool:
    host = (urlparse(str(url or "")).hostname or "").lower()
    return bool(host) and not any(host == name or host.endswith(f".{name}") for name in _SKIPPED_MEDIA_HOSTS)


def _usable_image(url: str) -> bool:
    if not _publishable_host(url):
        return False
    path = urlparse(url).path.lower()
    return not path.endswith((".svg", ".gif", ".webp", ".ico", ".avif", ".bmp"))


@mcp.tool(
    name="find_web_media",
    description=(
        "Lee una página HTTPS ya encontrada con search_web y devuelve fotos y videos "
        "que se pueden publicar, junto con el título y la URL de la fuente. "
        "Úsala para crear un post. No inventes otras URLs."
    ),
    structured_output=True,
)
async def find_web_media(url: str) -> dict[str, Any]:
    try:
        material = await fetch_source_content(
            str(url or "").strip(),
            timeout_seconds=settings.deepseek_source_timeout_seconds,
            max_bytes=settings.deepseek_source_max_bytes,
        )
    except (SourceFetchError, Exception) as exc:
        return {"ok": False, "url": url, "error": str(exc)}
    page_url = str(material.get("url") or url)
    ranked = [
        item
        for item in (material.get("image_urls") or [])
        if _usable_image(item) and _direct_file(item, (".jpg", ".jpeg", ".png"))
    ]
    ranked.extend(
        item
        for item in (material.get("image_urls") or [])
        if item not in ranked and _usable_image(item)
    )
    images = [{"url": item} for item in ranked[:4]]
    videos = [
        {"url": item}
        for item in (material.get("video_urls") or [])
        if _publishable_host(item) and _direct_file(item, (".mp4", ".mov"))
    ][:2]
    title = str(material.get("title") or "").strip()
    host = urlparse(page_url).hostname or ""
    return {
        "ok": True,
        "url": page_url,
        "title": title,
        "source_name": title or host,
        "text": str(material.get("text") or "")[:700],
        "images": images,
        "videos": videos,
    }


_BORING_PHOTO = (
    "logo",
    "mapa",
    "map",
    "bandera",
    "flag",
    "escudo",
    "icon",
    "icono",
    "diagrama",
    "diagram",
    "coat of arms",
)


@mcp.tool(
    name="search_tourism_photo",
    description=(
        "Busca una foto atractiva de turismo en Santiago o de un lugar concreto: "
        "calles, cerros, plazas, cafés, barrios. No devuelve mapas, logos ni banderas."
    ),
    structured_output=True,
)
async def search_tourism_photo(query: str) -> dict[str, Any]:
    text = str(query or "").strip()[:140]
    if not text:
        return {"ok": False, "query": text, "photos": [], "error": "La búsqueda está vacía."}
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"File:{text} Santiago",
        "gsrnamespace": "6",
        "gsrlimit": "12",
        "prop": "imageinfo",
        "iiprop": "url|mime",
        "iiurlwidth": "1400",
        "format": "json",
        "formatversion": "2",
    }
    try:
        async with httpx.AsyncClient(timeout=settings.deepseek_source_timeout_seconds) as client:
            response = await client.get(
                "https://commons.wikimedia.org/w/api.php",
                params=params,
                headers={"User-Agent": "BlackCatTourismBot/1.0 (+https://blackcathostal.com)"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "query": text, "photos": [], "error": str(exc)}
    photos: list[dict[str, str]] = []
    for page in (payload.get("query") or {}).get("pages") or []:
        if not isinstance(page, dict):
            continue
        title = str(page.get("title") or "").replace("File:", "").strip()
        if any(word in title.lower() for word in _BORING_PHOTO):
            continue
        info = (page.get("imageinfo") or [{}])[0]
        mime = str(info.get("mime") or "").lower()
        image_url = str(info.get("thumburl") or info.get("url") or "")
        source_url = str(info.get("descriptionurl") or "")
        if mime not in {"image/jpeg", "image/png", "image/webp"} or not image_url.startswith("https://"):
            continue
        photos.append(
            {
                "title": title[:180],
                "image_url": image_url,
                "source_url": source_url or "https://commons.wikimedia.org",
            }
        )
        if len(photos) >= 4:
            break
    if not photos:
        return {"ok": False, "query": text, "photos": [], "error": "No hay una foto atractiva para ese tema."}
    return {"ok": True, "query": text, "photos": photos}


def tool_result_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    return {}


async def call_reply_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one MCP tool for a DeepSeek Instagram or Facebook reply."""
    try:
        result = await mcp.call_tool(name, arguments or {})
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    payload = tool_result_payload(result)
    if payload:
        return payload
    return {"ok": False, "error": f"La tool {name} no devolvió datos."}


async def collect_google_places(queries: list[str]) -> list[dict[str, Any]]:
    """Run the Google Maps MCP tool for unique article-related queries."""
    def payload_from_result(result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result
        if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
            return result[1]
        structured = getattr(result, "structured_content", None)
        return structured if isinstance(structured, dict) else {}

    places: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in queries[:4]:
        normalized = str(query or "").strip()
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        result = await mcp.call_tool("search_google_places", {"query": normalized, "max_results": 5})
        payload = payload_from_result(result)
        if payload.get("ok"):
            places.extend(payload.get("places", []))
    unique_places: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for place in places:
        key = str(place.get("name") or "").strip().lower()
        if key and key not in seen_names:
            seen_names.add(key)
            unique_places.append(place)
    return unique_places


@mcp.tool(
    name="read_source",
    description="Lee el texto útil de una fuente activa configurada.",
    structured_output=True,
)
async def read_source(source_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        source = (
            db.query(AiSources)
            .filter(AiSources.id == int(source_id), AiSources.is_active.is_(True))
            .first()
        )
        if not source:
            return {"ok": False, "source_id": source_id, "error": "Fuente no encontrada o inactiva."}
        url = source.url
        source_name = source.name

    try:
        material = await fetch_source_content(
            url,
            timeout_seconds=settings.deepseek_source_timeout_seconds,
            max_bytes=settings.deepseek_source_max_bytes,
        )
    except Exception as exc:
        with SessionLocal() as db:
            failed = db.query(AiSources).filter(AiSources.id == int(source_id)).first()
            if failed:
                failed.last_checked_at = datetime.now(timezone.utc)
                failed.last_status = "error"
                failed.last_error = str(exc)[:1000]
                db.commit()
        return {"ok": False, "source_id": source_id, "name": source_name, "error": str(exc)}

    with SessionLocal() as db:
        checked = db.query(AiSources).filter(AiSources.id == int(source_id)).first()
        if checked:
            checked.last_checked_at = datetime.now(timezone.utc)
            checked.last_status = "ok"
            checked.last_error = None
            db.commit()

    return {
        "ok": True,
        "source_id": source_id,
        "name": source_name,
        "url": material["url"],
        "title": material["title"],
        "text": material["text"][: settings.deepseek_source_char_limit],
        "image_url": material.get("image_url", ""),
        "image_urls": material.get("image_urls", []),
        "image_candidates": material.get("image_candidates", []),
    }


async def collect_source_material() -> tuple[list[dict[str, Any]], list[int]]:
    def payload_from_result(result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result
        if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
            return result[1]
        structured = getattr(result, "structured_content", None)
        return structured if isinstance(structured, dict) else {}

    listed = await mcp.call_tool(
        "list_active_sources",
        {"limit": settings.deepseek_max_sources},
    )
    source_payload = payload_from_result(listed)
    materials: list[dict[str, Any]] = []
    source_ids: list[int] = []
    for source in source_payload.get("sources", []):
        result = await mcp.call_tool("read_source", {"source_id": int(source["id"])})
        result_payload = payload_from_result(result)
        if result_payload.get("ok"):
            materials.append(result_payload)
            source_ids.append(int(source["id"]))
    materials.extend(await collect_external_api_materials())
    return materials, source_ids


def protected_mcp_app():
    mcp_app = mcp.streamable_http_app()

    async def application(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            authorization = headers.get(b"authorization", b"").decode("latin-1")
            token = authorization.removeprefix("Bearer ").strip()
            authenticated = False
            if token:
                try:
                    payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
                    email = payload.get("sub")
                    with SessionLocal() as db:
                        user = (
                            db.query(Users)
                            .filter(Users.email == email, Users.is_active.is_(True))
                            .first()
                        )
                        authenticated = bool(user)
                except (jwt.PyJWTError, TypeError, ValueError):
                    authenticated = False
            if not authenticated:
                await JSONResponse(
                    {"detail": "Unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return
        await mcp_app(scope, receive, send)

    return application
