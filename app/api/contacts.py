from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.contact_groups import ContactGroups
from app.models.contacts import Contacts
from app.models.users import Users
from app.schemas.contacts import ContactsCreate, ContactsOut, ContactsUpdate

router = APIRouter(prefix="/contacts", tags=["contacts"])


def _validate_group(db: Session, group_id: int | None) -> None:
    if group_id is None:
        return
    group = db.query(ContactGroups).filter(ContactGroups.id == group_id).first()
    if not group:
        raise HTTPException(status_code=400, detail="El grupo seleccionado no existe")


@router.get("/", response_model=list[ContactsOut])
def list_contacts(
    active_only: bool = False,
    group_id: int | None = None,
    ungrouped: bool = False,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[Contacts]:
    query = db.query(Contacts).options(joinedload(Contacts.group)).outerjoin(ContactGroups)
    if active_only:
        query = query.filter(Contacts.is_active.is_(True))
    if ungrouped:
        query = query.filter(Contacts.group_id.is_(None))
    elif group_id is not None:
        query = query.filter(Contacts.group_id == group_id)
    return query.order_by(
        ContactGroups.name.asc(),
        Contacts.full_name.asc(),
        Contacts.id.asc(),
    ).all()


@router.get("/{contact_id}", response_model=ContactsOut)
def get_contact(
    contact_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Contacts:
    contact = (
        db.query(Contacts)
        .options(joinedload(Contacts.group))
        .filter(Contacts.id == contact_id)
        .first()
    )
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact


@router.post("/", response_model=ContactsOut, status_code=status.HTTP_201_CREATED)
def create_contact(
    payload: ContactsCreate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Contacts:
    email = payload.email.lower().strip()
    exists = db.query(Contacts).filter(Contacts.email == email).first()
    if exists:
        raise HTTPException(status_code=400, detail="Email already exists")

    _validate_group(db, payload.group_id)

    contact = Contacts(
        full_name=payload.full_name.strip(),
        email=email,
        is_active=payload.is_active,
        group_id=payload.group_id,
    )
    db.add(contact)
    db.commit()
    contact = (
        db.query(Contacts)
        .options(joinedload(Contacts.group))
        .filter(Contacts.id == contact.id)
        .first()
    )
    return contact


@router.put("/{contact_id}", response_model=ContactsOut)
def update_contact(
    contact_id: int,
    payload: ContactsUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Contacts:
    contact = db.query(Contacts).filter(Contacts.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    data = payload.model_dump(exclude_unset=True)
    if "email" in data and data["email"]:
        email = str(data["email"]).lower().strip()
        exists = (
            db.query(Contacts)
            .filter(Contacts.email == email, Contacts.id != contact_id)
            .first()
        )
        if exists:
            raise HTTPException(status_code=400, detail="Email already exists")
        data["email"] = email
    if "full_name" in data and data["full_name"]:
        data["full_name"] = data["full_name"].strip()
    if "group_id" in data:
        _validate_group(db, data["group_id"])

    for key, value in data.items():
        setattr(contact, key, value)

    db.commit()
    contact = (
        db.query(Contacts)
        .options(joinedload(Contacts.group))
        .filter(Contacts.id == contact_id)
        .first()
    )
    return contact


@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_contact(
    contact_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> None:
    contact = db.query(Contacts).filter(Contacts.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    db.delete(contact)
    db.commit()
