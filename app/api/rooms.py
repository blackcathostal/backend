from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.rooms import Rooms
from app.models.users import Users
from app.schemas.rooms import RoomsCreate, RoomsOut, RoomsUpdate

router = APIRouter(prefix="/rooms", tags=["rooms"])


@router.get("/", response_model=list[RoomsOut])
def list_rooms(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[Rooms]:
    return db.query(Rooms).order_by(Rooms.id.desc()).all()


@router.get("/{room_id}", response_model=RoomsOut)
def get_room(
    room_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Rooms:
    room = db.query(Rooms).filter(Rooms.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return room


@router.post("/", response_model=RoomsOut, status_code=status.HTTP_201_CREATED)
def create_room(
    payload: RoomsCreate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Rooms:
    room = Rooms(
        name=payload.name.strip(),
        type=payload.type.strip(),
        capacity=payload.capacity,
        price=payload.price,
        status=(payload.status or "Disponible").strip(),
    )
    db.add(room)
    db.commit()
    db.refresh(room)
    return room


@router.put("/{room_id}", response_model=RoomsOut)
def update_room(
    room_id: int,
    payload: RoomsUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Rooms:
    room = db.query(Rooms).filter(Rooms.id == room_id).first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    data = payload.model_dump(exclude_unset=True)
    for key in ("name", "type", "status"):
        if key in data and data[key] is not None:
            data[key] = str(data[key]).strip()

    for key, value in data.items():
        setattr(room, key, value)

    db.commit()
    db.refresh(room)
    return room


@router.delete("/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
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
