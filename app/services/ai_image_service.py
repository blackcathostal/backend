from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
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
) -> tuple[str, str]:
    """Download a new safe source image and store it in the post uploads."""
    excluded = excluded_urls or set()
    existing_fingerprints = _existing_image_fingerprints(excluded_paths or set())
    for material in materials:
        candidates = material.get("image_urls") or [material.get("image_url")]
        for candidate in candidates:
            image_url = str(candidate or "").strip()
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
