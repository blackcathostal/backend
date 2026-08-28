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
        "iiprop": "url|mime",
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
    candidates: list[tuple[int, dict[str, str]]] = []
    blocked_terms = (
        "logo", "logotipo", "emblema", "bandera", "flag", "mapa", "map ",
        "icono", "icon", "escudo", "seal", "poster", "afiche", "diagrama",
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
        score = len(query_words & _content_words(title))
        candidates.append(
            (
                score,
                {"image_url": image_url, "source_url": source_url},
            )
        )
    return [candidate for _, candidate in sorted(candidates, key=lambda item: item[0], reverse=True)]


def _search_query(value: str) -> str:
    words = re.findall(r"[a-záéíóúñ]{4,}", value.lower())
    ignored = {
        "para", "desde", "entre", "sobre", "este", "esta", "como", "donde",
        "hacia", "hasta", "también", "puede", "pueden", "artículo", "articulos",
        "turismo", "guía", "guia", "días", "dias",
    }
    unique = list(dict.fromkeys(word for word in words if word not in ignored))
    prefixes = {
        "barrio", "cerro", "cerros", "museo", "palacio", "plaza",
        "parque", "mercado", "mirador", "costanera",
    }
    for index, word in enumerate(unique):
        if word in prefixes and index + 1 < len(unique):
            return f"{word} {unique[index + 1]} Santiago"
    topical = unique[:3]
    return " ".join(topical + ["Santiago"]) if topical else "Santiago Chile turismo"


def _content_words(value: str) -> set[str]:
    stopwords = {
        "para", "desde", "entre", "sobre", "este", "esta", "como", "donde",
        "hacia", "hasta", "también", "puede", "pueden", "santiago", "chile",
    }
    return {
        word
        for word in re.findall(r"[a-záéíóúñ]{5,}", value.lower())
        if word not in stopwords
    }


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
