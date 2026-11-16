from __future__ import annotations

import json
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.rooms import Rooms
from app.models.users import Users
from app.schemas.rooms import RoomsCreate, RoomsOut, RoomsUpdate
from app.services.images import save_upload_as_webp

router = APIRouter(prefix="/rooms", tags=["rooms"])

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_ROOM_PHOTOS = 6


def _photos_list(room: Rooms) -> list[str]:
    """Return exactly 6 positional slots (empty string = empty box)."""
    raw = room.photos or "[]"
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        loaded = []
    if not isinstance(loaded, list):
        loaded = []
    slots = [str(item or "").strip() for item in loaded][:MAX_ROOM_PHOTOS]
    while len(slots) < MAX_ROOM_PHOTOS:
        slots.append("")
    return slots


def _set_photos(room: Rooms, photos: list[str]) -> None:
    slots = [str(item or "").strip() for item in photos][:MAX_ROOM_PHOTOS]
    while len(slots) < MAX_ROOM_PHOTOS:
        slots.append("")
    room.photos = json.dumps(slots, ensure_ascii=False)


def _serialize(room: Rooms) -> RoomsOut:
    stored = _photos_list(room)
    while len(stored) < MAX_ROOM_PHOTOS:
        stored.append("")
    return RoomsOut(
        id=room.id,
        name=room.name,
        type=room.type,
        capacity=room.capacity,
        price=room.price,
        features=room.features or "",
        photos=stored[:MAX_ROOM_PHOTOS],
        created_at=room.created_at,
        updated_at=room.updated_at,
    )


@router.get("/", response_model=list[RoomsOut])
def list_rooms(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[RoomsOut]:
    rows = db.query(Rooms).order_by(Rooms.id.desc()).all()
    return [_serialize(row) for row in rows]


@router.get("/{room_id}", response_model=RoomsOut)
def get_room(
    room_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> RoomsOut:
    room = db.query(Rooms).filter(Rooms.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return _serialize(room)


@router.post("/", response_model=RoomsOut, status_code=status.HTTP_201_CREATED)
def create_room(
    payload: RoomsCreate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> RoomsOut:
    room = Rooms(
        name=payload.name.strip(),
        type=payload.type.strip(),
        capacity=payload.capacity,
        price=payload.price,
        features=(payload.features or "").strip(),
        photos=json.dumps([""] * MAX_ROOM_PHOTOS),
    )
    db.add(room)
    db.commit()
    db.refresh(room)
    return _serialize(room)


@router.put("/{room_id}", response_model=RoomsOut)
def update_room(
    room_id: int,
    payload: RoomsUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> RoomsOut:
    room = db.query(Rooms).filter(Rooms.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    data = payload.model_dump(exclude_unset=True)
    for key in ("name", "type", "features"):
        if key in data and data[key] is not None:
            data[key] = str(data[key]).strip()

    for key, value in data.items():
        setattr(room, key, value)

    db.commit()
    db.refresh(room)
    return _serialize(room)


@router.post("/{room_id}/photos/{slot}", response_model=RoomsOut)
async def upload_room_photo(
    room_id: int,
    slot: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> RoomsOut:
    if slot < 0 or slot >= MAX_ROOM_PHOTOS:
        raise HTTPException(status_code=400, detail=f"Slot debe ser 0–{MAX_ROOM_PHOTOS - 1}")
    room = db.query(Rooms).filter(Rooms.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Formato de imagen no permitido")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Archivo vacío")

    filename = f"room-{room_id}-{slot}-{uuid4().hex}.webp"
    destination = settings.uploads_dir / "rooms" / filename
    save_upload_as_webp(content, destination)

    photos = _photos_list(room)
    while len(photos) < MAX_ROOM_PHOTOS:
        photos.append("")
    old = photos[slot]
    photos[slot] = f"/uploads/rooms/{filename}"
    _set_photos(room, photos)
    db.commit()
    db.refresh(room)

    if old and old.startswith("/uploads/rooms/"):
        old_path = settings.uploads_dir / old.split("/uploads/", 1)[-1]
        try:
            if old_path.is_file():
                old_path.unlink()
        except OSError:
            pass

    return _serialize(room)


@router.delete("/{room_id}/photos/{slot}", response_model=RoomsOut)
def delete_room_photo(
    room_id: int,
    slot: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> RoomsOut:
    if slot < 0 or slot >= MAX_ROOM_PHOTOS:
        raise HTTPException(status_code=400, detail=f"Slot debe ser 0–{MAX_ROOM_PHOTOS - 1}")
    room = db.query(Rooms).filter(Rooms.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    photos = _photos_list(room)
    while len(photos) < MAX_ROOM_PHOTOS:
        photos.append("")
    old = photos[slot]
    photos[slot] = ""
    _set_photos(room, photos)
    db.commit()
    db.refresh(room)

    if old and old.startswith("/uploads/rooms/"):
        old_path = settings.uploads_dir / old.split("/uploads/", 1)[-1]
        try:
            if old_path.is_file():
                old_path.unlink()
        except OSError:
            pass

    return _serialize(room)


@router.delete("/{room_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_room(
    room_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> None:
    room = db.query(Rooms).filter(Rooms.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    db.delete(room)
    db.commit()
