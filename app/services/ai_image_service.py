from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
from uuid import uuid4

import httpx
from PIL import Image

from app.core.config import settings
from app.services.ai_source_fetcher import SourceFetchError, download_image
from app.services.images import save_upload_as_webp


async def download_free_image(
    excluded_urls: set[str] | None = None,
    excluded_paths: set[str] | None = None,
    relevance_text: str = "",
) -> tuple[str, str]:
    """Find a relevant free Wikimedia Commons photo and store it locally."""
    excluded = excluded_urls or set()
    existing_fingerprints = _existing_image_fingerprints(excluded_paths or set())
    candidates = await _search_free_images(relevance_text)
    for candidate in candidates:
        image_url = candidate["image_url"]
        source_url = candidate["source_url"]
        if source_url in excluded or image_url in excluded:
            continue
        try:
            content, _ = await download_image(
                image_url,
                timeout_seconds=settings.deepseek_source_timeout_seconds,
                max_bytes=settings.deepseek_image_max_bytes,
            )
            if _image_fingerprint(content) in existing_fingerprints:
                continue
            filename = f"ai-post-{uuid4().hex}.webp"
            destination = settings.uploads_dir / "posts" / filename
            save_upload_as_webp(content, destination)
            return f"/uploads/posts/{filename}", source_url
        except (SourceFetchError, OSError, ValueError, httpx.HTTPError):
            continue
    raise SourceFetchError("No se encontró una fotografía libre y relevante para el artículo.")


async def _search_free_images(relevance_text: str) -> list[dict[str, str]]:
    query = _search_query(relevance_text)
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"File:{query}",
        "gsrnamespace": "6",
        "gsrlimit": "30",
        "prop": "imageinfo",
        "iiprop": "url|mime|timestamp|extmetadata",
        "iiurlwidth": "1600",
        "format": "json",
        "formatversion": "2",
        "origin": "*",
    }
    headers = {
        "User-Agent": (
            "BlackCatTourismBot/1.0 "
            "(https://blackcathostal.com; contacto@blackcathostal.com)"
        ),
        "Accept": "application/json",
        "Accept-Language": "es,en;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=settings.deepseek_source_timeout_seconds) as client:
            response = await client.get(settings.image_search_api_url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceFetchError("No se pudo buscar una fotografía libre.") from exc

    query_words = _content_words(query)
    title_words = _title_words(relevance_text)
    anchor_words = _title_anchor_words(relevance_text)
    coastal_topic = bool(
        {"playa", "playas", "costa", "costero", "mar", "oceano", "océano", "beach"}
        & _content_words(relevance_text)
    )
    candidates: list[tuple[int, dict[str, str]]] = []
    blocked_terms = (
        "logo", "logotipo", "emblema", "bandera", "flag", "mapa", "map ",
        "icono", "icon", "escudo", "seal", "poster", "afiche", "diagrama",
        "ceremonia", "protocolaria", "conmemorar", "politician", "political",
        "politico", "político", "discurso", "speech", "portrait", "retrato",
        "election", "elección", "senator", "senador", "diputado", "ministro",
        "alcalde", "parliament", "parlamento", "congreso", "speaker",
        "espectaculo", "espectáculo", "musical", "performer", "performers",
        "actriz", "actor", "mujer", "woman", "man", "person", "people",
        "crowd", "programa", "festival", "concierto",
    )
    for page in (payload.get("query", {}).get("pages", []) or []):
        title = str(page.get("title") or "")
        normalized_title = title.lower()
        if any(term in normalized_title for term in blocked_terms):
            continue
        info = (page.get("imageinfo") or [{}])[0]
        mime = str(info.get("mime") or "").lower()
        image_url = str(info.get("thumburl") or info.get("url") or "")
        source_url = str(info.get("descriptionurl") or "")
        if mime not in {"image/jpeg", "image/png", "image/webp"} or not image_url or not source_url:
            continue
        metadata = info.get("extmetadata") or {}
        if not _is_recent_enough(info.get("timestamp")):
            continue
        original_date = str(
            (metadata.get("DateTimeOriginal") or {}).get("value") or ""
        )
        original_year = re.match(r"(\d{4})", original_date)
        if original_year and int(original_year.group(1)) < settings.ai_image_min_year:
            continue
        metadata_text = " ".join(
            str(metadata.get(key, {}).get("value") or "")
            for key in ("ImageDescription", "ObjectName", "Categories")
            if isinstance(metadata.get(key), dict)
        )
        image_title_words = _content_words(title)
        metadata_words = _content_words(metadata_text)
        title_matches = len(title_words & image_title_words)
        anchor_matches = len(anchor_words & image_title_words)
        if title_words and title_matches < 1:
            continue
        if anchor_words and anchor_matches < 1:
            continue
        candidate_text = f"{title} {metadata_text}"
        if not coastal_topic and _contains_any(
            candidate_text,
            ("playa", "playas", "beach", "mar", "ocean", "océano", "oceanfront"),
        ):
            continue
        score = (
            title_matches * 20
            + anchor_matches * 40
            + len(query_words & image_title_words) * 3
            + len(query_words & metadata_words)
            + (10 if _normalized_phrase_match(relevance_text, title) else 0)
        )
        candidates.append(
            (
                score,
                {"image_url": image_url, "source_url": source_url},
            )
        )
    return [candidate for _, candidate in sorted(candidates, key=lambda item: item[0], reverse=True)]


def _search_query(value: str) -> str:
    title_match = re.search(r"título:\s*([^\n]+)", value, flags=re.IGNORECASE)
    places_match = re.search(r"lugares:\s*([^\n]+)", value, flags=re.IGNORECASE)
    search_value = title_match.group(1) if title_match else value
    words = re.findall(r"[a-záéíóúñ]{3,}", search_value.lower())
    ignored = {
        "para", "desde", "entre", "sobre", "este", "esta", "como", "donde",
        "hacia", "hasta", "también", "puede", "pueden", "artículo", "articulos",
        "turismo", "guía", "guia", "días", "dias",
        "vista", "vistas", "panoramica", "panorámica", "visitar", "visita",
        "descubre", "conoce", "experiencia", "recorrido", "ruta",
    }
    unique = list(dict.fromkeys(word for word in words if word not in ignored))
    if title_match:
        anchors = [
            word.lower()
            for word in re.findall(r"\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,}\b", title_match.group(1))
            if word.lower() not in {"santiago", "chile", "día", "dia"}
        ]
        if anchors:
            return " ".join(anchors[:3] + ["Santiago"])
        if places_match:
            place_words = [
                word for word in re.findall(r"[a-záéíóúñ]{3,}", places_match.group(1).lower())
                if word not in ignored and word != "santiago"
            ]
            if place_words:
                return " ".join(place_words[:3] + ["Santiago"])
        topical = [word for word in unique if word != "santiago"][:3]
        return " ".join(topical + ["Santiago"]) if topical else "Santiago Chile turismo"
    prefixes = {
        "barrio", "cerro", "cerros", "museo", "palacio", "plaza",
        "parque", "mercado", "mirador", "costanera",
    }
    for index, word in enumerate(unique):
        if word in prefixes and index + 1 < len(unique):
            return f"{word} {unique[index + 1]} Santiago"
    topical = unique[:5]
    return " ".join(topical + ["Santiago"]) if topical else "Santiago Chile turismo"


def _content_words(value: str) -> set[str]:
    stopwords = {
        "para", "desde", "entre", "sobre", "este", "esta", "como", "donde",
        "hacia", "hasta", "también", "puede", "pueden", "santiago", "chile",
        "sobre", "donde", "cómo", "como", "guía", "guia", "visita", "visitar",
        "descubre", "conoce", "mejor", "lugares", "lugar", "turístico", "turismo",
    }
    return {
        word
        for word in re.findall(r"[a-záéíóúñ]{3,}", value.lower())
        if word not in stopwords
    }


def _title_words(value: str) -> set[str]:
    match = re.search(r"título:\s*([^\n]+)", value, flags=re.IGNORECASE)
    title = match.group(1) if match else value
    return _content_words(title)


def _title_anchor_words(value: str) -> set[str]:
    match = re.search(r"título:\s*([^\n]+)", value, flags=re.IGNORECASE)
    places_match = re.search(r"lugares:\s*([^\n]+)", value, flags=re.IGNORECASE)
    if not match and not places_match:
        return set()
    ignored = {"santiago", "chile", "día", "dia"}
    title_anchors = (
        {
            word.lower()
            for word in re.findall(r"\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,}\b", match.group(1))
            if word.lower() not in ignored
        }
        if match
        else set()
    )
    if title_anchors:
        return title_anchors
    return {
        word
        for word in _content_words(places_match.group(1))
        if word not in ignored
    }


def _contains_any(value: str, terms: tuple[str, ...]) -> bool:
    normalized = value.lower()
    return any(re.search(rf"\b{re.escape(term)}\b", normalized) for term in terms)


def _is_recent_enough(value: str | None) -> bool:
    match = re.match(r"(\d{4})", str(value or ""))
    return bool(match and int(match.group(1)) >= settings.ai_image_min_year)


def _normalized_phrase_match(relevance_text: str, value: str) -> bool:
    title_match = re.search(r"título:\s*([^\n]+)", relevance_text, flags=re.IGNORECASE)
    if not title_match:
        return False
    title_words = re.findall(r"[a-záéíóúñ]{3,}", title_match.group(1).lower())
    phrase = " ".join(title_words[:5])
    normalized = re.sub(r"[^a-záéíóúñ ]+", " ", value.lower())
    return len(title_words) >= 2 and phrase in normalized


def _image_fingerprint(content: bytes) -> str:
    with Image.open(BytesIO(content)) as image:
        normalized = image.convert("RGB")
        return sha256(
            f"{normalized.size[0]}x{normalized.size[1]}".encode() + normalized.tobytes()
        ).hexdigest()


def _existing_image_fingerprints(image_urls: set[str]) -> set[str]:
    fingerprints: set[str] = set()
    for image_url in image_urls:
        if not image_url.startswith("/uploads/posts/"):
            continue
        path = settings.uploads_dir / "posts" / Path(image_url).name
        try:
            fingerprints.add(_image_fingerprint(path.read_bytes()))
        except (OSError, ValueError):
            continue
    return fingerprints
