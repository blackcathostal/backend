from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.medias import Medias
from app.models.users import Users
from app.schemas.media import MediasOut, MediasUpdate
from app.services.images import save_upload_as_webp

router = APIRouter(prefix="/medias", tags=["medias"])

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@router.get("/", response_model=list[MediasOut])
def list_media(category: str | None = None, db: Session = Depends(get_db)) -> list[Medias]:
    query = db.query(Medias)
    if category:
        query = query.filter(Medias.category == category)
    return query.order_by(Medias.id.desc()).all()


@router.post("/upload", response_model=MediasOut, status_code=status.HTTP_201_CREATED)
async def upload_media(
    file: UploadFile = File(...),
    category: str = Form("general"),
    alt_text: str = Form(""),
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Medias:
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Formato de imagen no permitido")

    filename = f"{uuid4().hex}.webp"
    destination = settings.uploads_dir / "media" / filename
    content = await file.read()
    save_upload_as_webp(content, destination)

    media = Medias(
        filename=(Path(file.filename or filename).stem + ".webp"),
        url=f"/uploads/media/{filename}",
        category=category or "general",
        alt_text=alt_text or "",
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


@router.put("/{media_id}", response_model=MediasOut)
def update_media(
    media_id: int,
    payload: MediasUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Medias:
    media = db.query(Medias).filter(Medias.id == media_id).first()
    if not media:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(media, key, value)

    db.commit()
    db.refresh(media)
    return media


@router.delete("/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_media(
    media_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> None:
    media = db.query(Medias).filter(Medias.id == media_id).first()
    if not media:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    if media.url.startswith("/uploads/"):
        relative = media.url.removeprefix("/uploads/").lstrip("/")
        file_path = settings.uploads_dir / relative
        if file_path.exists() and file_path.is_file():
            file_path.unlink()

    db.delete(media)
    db.commit()
