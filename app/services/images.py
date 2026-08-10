from io import BytesIO
from pathlib import Path

from PIL import Image


def save_upload_as_webp(content: bytes, destination: Path, quality: int = 82) -> Path:
    """Convert uploaded image bytes to WebP and write to destination (.webp)."""
    destination = destination.with_suffix(".webp")
    destination.parent.mkdir(parents=True, exist_ok=True)

    image = Image.open(BytesIO(content))
    if image.mode in ("P", "LA"):
        image = image.convert("RGBA")
    elif image.mode == "CMYK":
        image = image.convert("RGB")
    elif image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")

    image.save(destination, "WEBP", quality=quality, method=6)
    return destination
