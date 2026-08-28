from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx

from app.core.config import settings
from app.services.ai_source_fetcher import SourceFetchError, download_image
from app.services.images import save_upload_as_webp


async def download_source_image(materials: list[dict[str, Any]]) -> str:
    """Download the first safe source image and store it in the post uploads."""
    for material in materials:
        image_url = str(material.get("image_url") or "").strip()
        if not image_url:
            continue
        try:
            content, _ = await download_image(
                image_url,
                timeout_seconds=settings.deepseek_source_timeout_seconds,
                max_bytes=settings.deepseek_image_max_bytes,
            )
            filename = f"ai-post-{uuid4().hex}.webp"
            destination = settings.uploads_dir / "posts" / filename
            save_upload_as_webp(content, destination)
            return f"/uploads/posts/{filename}"
        except (SourceFetchError, OSError, ValueError, httpx.HTTPError):
            continue
    return ""
