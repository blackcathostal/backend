from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.contact_groups import ContactGroups
from app.models.contacts import Contacts
from app.models.users import Users
from app.schemas.contact_groups import (
    ContactGroupsCreate,
    ContactGroupsOut,
    ContactGroupsUpdate,
)

router = APIRouter(prefix="/contact-groups", tags=["contact-groups"])


@router.get("/", response_model=list[ContactGroupsOut])
def list_contact_groups(
    active_only: bool = False,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[ContactGroups]:
    query = db.query(ContactGroups)
    if active_only:
        query = query.filter(ContactGroups.is_active.is_(True))
    return query.order_by(ContactGroups.name.asc(), ContactGroups.id.asc()).all()


@router.get("/{group_id}", response_model=ContactGroupsOut)
def get_contact_group(
    group_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> ContactGroups:
    group = db.query(ContactGroups).filter(ContactGroups.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Grupo no encontrado")
    return group


@router.post("/", response_model=ContactGroupsOut, status_code=status.HTTP_201_CREATED)
def create_contact_group(
    payload: ContactGroupsCreate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> ContactGroups:
    name = payload.name.strip()
    exists = db.query(ContactGroups).filter(ContactGroups.name == name).first()
    if exists:
        raise HTTPException(status_code=400, detail="Ya existe un grupo con ese nombre")

    group = ContactGroups(
        name=name,
        description=(payload.description or "").strip(),
        is_active=payload.is_active,
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return group


@router.put("/{group_id}", response_model=ContactGroupsOut)
def update_contact_group(
    group_id: int,
    payload: ContactGroupsUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> ContactGroups:
    group = db.query(ContactGroups).filter(ContactGroups.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Grupo no encontrado")

    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        name = str(data["name"]).strip()
        exists = (
            db.query(ContactGroups)
            .filter(ContactGroups.name == name, ContactGroups.id != group_id)
            .first()
        )
        if exists:
            raise HTTPException(status_code=400, detail="Ya existe un grupo con ese nombre")
        data["name"] = name
    if "description" in data and data["description"] is not None:
        data["description"] = str(data["description"]).strip()

    for key, value in data.items():
        setattr(group, key, value)

    db.commit()
    db.refresh(group)
    return group


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_contact_group(
    group_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> None:
    group = db.query(ContactGroups).filter(ContactGroups.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Grupo no encontrado")

    linked = db.query(Contacts).filter(Contacts.group_id == group_id).count()
    if linked:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No se puede eliminar: hay {linked} destinatario(s) en este grupo. "
                "Muévelos a otro grupo o quítales el grupo antes."
            ),
        )

    db.delete(group)
    db.commit()
