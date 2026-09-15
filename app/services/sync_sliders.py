from pathlib import Path
from shutil import copy2

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.medias import Medias
from app.models.sliders import Sliders

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_SLIDER_DIR = PROJECT_ROOT / "frontend" / "public" / "cappa" / "img" / "slider"

FRONTEND_SLIDERS = [
    {
        "eyebrow": "Santiago y Los Andes",
        "title": "Descubre la Ciudad de la Nieve",
        "source": FRONTEND_SLIDER_DIR / "santiago" / "1.webp",
        "overlay": 3,
        "sort_order": 1,
    },
    {
        "eyebrow": "Experiencia Valle Nevado",
        "title": "Vive la Magia de la Nieve",
        "source": FRONTEND_SLIDER_DIR / "santiago" / "2.webp",
        "overlay": 2,
        "sort_order": 2,
    },
    {
        "eyebrow": "Centro Hist\u00f3rico de Santiago",
        "title": "Explora la Ciudad Tur\u00edstica",
        "source": FRONTEND_SLIDER_DIR / "santiago" / "3.webp",
        "overlay": 3,
        "sort_order": 3,
    },
    {
        "eyebrow": "Vistas desde el San Crist\u00f3bal",
        "title": "Ciudad, Monta\u00f1as y Cultura",
        "source": FRONTEND_SLIDER_DIR / "santiago" / "4.webp",
        "overlay": 3,
        "sort_order": 4,
    },
]


def _public_slider_url(source: Path, sort_order: int) -> str | None:
    """Prefer stable public frontend assets; optionally mirror into uploads for local admin."""
    if not source.exists():
        return None

    # Public site serves these from Firebase; production API may not have /uploads/sliders.
    public_url = f"/cappa/img/slider/santiago/{sort_order}{source.suffix.lower()}"

    try:
        destination_dir = settings.uploads_dir / "sliders"
        destination_dir.mkdir(parents=True, exist_ok=True)
        filename = f"frontend-santiago-{sort_order}{source.suffix.lower()}"
        copy2(source, destination_dir / filename)
    except OSError:
        pass

    return public_url


def sync_frontend_sliders(db: Session) -> list[Sliders]:
    """Point sliders at public /cappa assets and upsert texts in DB."""
    synced: list[Sliders] = []

    for item in FRONTEND_SLIDERS:
        image_url = _public_slider_url(item["source"], item["sort_order"])
        if not image_url:
            continue

        slider = (
            db.query(Sliders)
            .filter(Sliders.sort_order == item["sort_order"])
            .order_by(Sliders.id.asc())
            .first()
        )
        if not slider:
            slider = Sliders(sort_order=item["sort_order"])
            db.add(slider)

        slider.eyebrow = item["eyebrow"]
        slider.title = item["title"]
        slider.image_url = image_url
        slider.overlay = item["overlay"]
        slider.is_active = True
        synced.append(slider)

        media = db.query(Medias).filter(Medias.url == image_url).first()
        if not media:
            db.add(
                Medias(
                    filename=Path(image_url).name,
                    url=image_url,
                    category="slider",
                    alt_text=item["title"],
                )
            )

    db.commit()
    for slider in synced:
        db.refresh(slider)
    return synced
