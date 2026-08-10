import re
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.posts import Posts
from app.models.users import Users
from app.schemas.post import PostsCreate, PostsOut, PostsUpdate
from app.services.images import save_upload_as_webp

router = APIRouter(prefix="/posts", tags=["posts"])

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[áàäâ]", "a", value)
    value = re.sub(r"[éèëê]", "e", value)
    value = re.sub(r"[íìïî]", "i", value)
    value = re.sub(r"[óòöô]", "o", value)
    value = re.sub(r"[úùüû]", "u", value)
    value = re.sub(r"ñ", "n", value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "articulo"


def ensure_unique_slug(db: Session, slug: str, exclude_id: int | None = None) -> str:
    base = slugify(slug)
    candidate = base
    index = 2
    while True:
        query = db.query(Posts).filter(Posts.slug == candidate)
        if exclude_id is not None:
            query = query.filter(Posts.id != exclude_id)
        if not query.first():
            return candidate
        candidate = f"{base}-{index}"
        index += 1


@router.get("/", response_model=list[PostsOut])
def list_posts(active_only: bool = False, db: Session = Depends(get_db)) -> list[Posts]:
    query = db.query(Posts)
    if active_only:
        query = query.filter(Posts.is_active.is_(True))
    return query.order_by(Posts.published_at.desc(), Posts.sort_order.asc(), Posts.id.desc()).all()


@router.get("/by-slug/{slug}", response_model=PostsOut)
def get_post_by_slug(slug: str, db: Session = Depends(get_db)) -> Posts:
    post = db.query(Posts).filter(Posts.slug == slug, Posts.is_active.is_(True)).first()
    if not post:
        raise HTTPException(status_code=404, detail="Artículo no encontrado")
    return post


@router.get("/{post_id}", response_model=PostsOut)
def get_post(post_id: int, db: Session = Depends(get_db)) -> Posts:
    post = db.query(Posts).filter(Posts.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Artículo no encontrado")
    return post


@router.post("/", response_model=PostsOut, status_code=status.HTTP_201_CREATED)
def create_post(
    payload: PostsCreate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Posts:
    data = payload.model_dump()
    data["slug"] = ensure_unique_slug(db, data.get("slug") or data["title"])
    post = Posts(**data)
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


@router.put("/{post_id}", response_model=PostsOut)
def update_post(
    post_id: int,
    payload: PostsUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Posts:
    post = db.query(Posts).filter(Posts.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Artículo no encontrado")

    data = payload.model_dump(exclude_unset=True)
    if "slug" in data and data["slug"]:
        data["slug"] = ensure_unique_slug(db, data["slug"], exclude_id=post_id)
    elif "title" in data and data["title"] and not data.get("slug"):
        # keep existing slug unless explicitly changed
        pass

    for key, value in data.items():
        setattr(post, key, value)

    db.commit()
    db.refresh(post)
    return post


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_post(
    post_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> None:
    post = db.query(Posts).filter(Posts.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Artículo no encontrado")
    db.delete(post)
    db.commit()


@router.post("/{post_id}/image", response_model=PostsOut)
async def upload_post_image(
    post_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Posts:
    post = db.query(Posts).filter(Posts.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Artículo no encontrado")

    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Formato de imagen no permitido")

    filename = f"post-{post_id}-{uuid4().hex}.webp"
    destination = settings.uploads_dir / "posts" / filename
    content = await file.read()
    save_upload_as_webp(content, destination)

    post.image_url = f"/uploads/posts/{filename}"
    db.commit()
    db.refresh(post)
    return post
