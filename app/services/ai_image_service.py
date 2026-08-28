from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

import httpx
from PIL import Image

from app.core.config import settings
from app.services.ai_source_fetcher import SourceFetchError, download_image
from app.services.images import save_upload_as_webp


async def download_source_image(
    materials: list[dict[str, Any]],
    excluded_urls: set[str] | None = None,
    excluded_paths: set[str] | None = None,
    relevance_text: str = "",
) -> tuple[str, str]:
    """Download a new safe source image and store it in the post uploads."""
    excluded = excluded_urls or set()
    existing_fingerprints = _existing_image_fingerprints(excluded_paths or set())
    relevance_words = _content_words(relevance_text)
    for material in materials:
        candidates = material.get("image_candidates") or [
            {"url": candidate, "alt": ""} for candidate in (material.get("image_urls") or [])
        ]
        if not candidates and material.get("image_url"):
            candidates = [{"url": material["image_url"], "alt": ""}]
        candidates = sorted(
            candidates,
            key=lambda candidate: len(
                relevance_words
                & _content_words(
                    f"{candidate.get('url', '')} {candidate.get('alt', '')} "
                    f"{material.get('title', '')}"
                )
            ),
            reverse=True,
        )
        for candidate in candidates:
            image_url = str(candidate.get("url") if isinstance(candidate, dict) else candidate).strip()
            if not image_url or image_url in excluded:
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
                return f"/uploads/posts/{filename}", image_url
            except (SourceFetchError, OSError, ValueError, httpx.HTTPError):
                continue
    raise SourceFetchError("No se encontró una imagen nueva y válida en las fuentes.")


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
