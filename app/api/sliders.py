from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.sliders import Sliders
from app.models.users import Users
from app.schemas.slider import SlidersCreate, SlidersOut, SlidersReorderItem, SlidersUpdate
from app.services.images import save_upload_as_webp

router = APIRouter(prefix="/sliders", tags=["sliders"])

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
FRONTEND_SLIDER_DIR = (
    Path(__file__).resolve().parents[3] / "frontend" / "public" / "cappa" / "img" / "slider"
)


def frontend_slider_assets() -> list[dict[str, str]]:
    if not FRONTEND_SLIDER_DIR.exists():
        return []

    assets: list[dict[str, str]] = []
    for path in sorted(FRONTEND_SLIDER_DIR.rglob("*.webp")):
        relative = path.relative_to(FRONTEND_SLIDER_DIR).as_posix()
        assets.append(
            {
                "name": relative,
                "url": f"/cappa/img/slider/{relative}",
            }
        )
    return assets


@router.get("/assets")
def list_slider_assets(_: Users = Depends(get_current_user)) -> list[dict[str, str]]:
    return frontend_slider_assets()


@router.post("/import-frontend", response_model=list[SlidersOut])
def import_frontend_sliders(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[Sliders]:
    from app.services.sync_sliders import sync_frontend_sliders

    return sync_frontend_sliders(db)


@router.get("/", response_model=list[SlidersOut])
def list_sliders(active_only: bool = False, db: Session = Depends(get_db)) -> list[Sliders]:
    query = db.query(Sliders)
    if active_only:
        query = query.filter(Sliders.is_active.is_(True))
    return query.order_by(Sliders.sort_order.asc(), Sliders.id.asc()).all()


@router.get("/{slider_id}", response_model=SlidersOut)
def get_slider(slider_id: int, db: Session = Depends(get_db)) -> Sliders:
    slider = db.query(Sliders).filter(Sliders.id == slider_id).first()
    if not slider:
        raise HTTPException(status_code=404, detail="Slider not found")
    return slider


@router.post("/", response_model=SlidersOut, status_code=status.HTTP_201_CREATED)
def create_slider(
    payload: SlidersCreate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Sliders:
    slider = Sliders(**payload.model_dump())
    db.add(slider)
    db.commit()
    db.refresh(slider)
    return slider


@router.put("/{slider_id}", response_model=SlidersOut)
def update_slider(
    slider_id: int,
    payload: SlidersUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Sliders:
    slider = db.query(Sliders).filter(Sliders.id == slider_id).first()
    if not slider:
        raise HTTPException(status_code=404, detail="Slider not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(slider, key, value)

    db.commit()
    db.refresh(slider)
    return slider


@router.delete("/{slider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_slider(
    slider_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> None:
    slider = db.query(Sliders).filter(Sliders.id == slider_id).first()
    if not slider:
        raise HTTPException(status_code=404, detail="Slider not found")
    db.delete(slider)
    db.commit()


@router.post("/reorder", response_model=list[SlidersOut])
def reorder_sliders(
    items: list[SlidersReorderItem],
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[Sliders]:
    by_id = {item.id: item.sort_order for item in items}
    sliders = db.query(Sliders).filter(Sliders.id.in_(by_id.keys())).all()
    for slider in sliders:
        slider.sort_order = by_id[slider.id]
    db.commit()
    return db.query(Sliders).order_by(Sliders.sort_order.asc(), Sliders.id.asc()).all()


@router.post("/{slider_id}/image", response_model=SlidersOut)
async def upload_slider_image(
    slider_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Sliders:
    slider = db.query(Sliders).filter(Sliders.id == slider_id).first()
    if not slider:
        raise HTTPException(status_code=404, detail="Slider not found")

    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Image format not allowed")

    filename = f"slider-{slider_id}-{uuid4().hex}.webp"
    destination = settings.uploads_dir / "sliders" / filename
    content = await file.read()
    save_upload_as_webp(content, destination)

    slider.image_url = f"/uploads/sliders/{filename}"
    db.commit()
    db.refresh(slider)
    return slider
