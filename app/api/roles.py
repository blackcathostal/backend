from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.permissions import Permissions, RolePermissions
from app.models.roles import Roles
from app.models.users import Users
from app.schemas.roles import (
    PermissionCreate,
    PermissionOut,
    PermissionUpdate,
    RoleCreate,
    RoleOut,
    RoleUpdate,
)

router = APIRouter(tags=["roles-permissions"])


def _serialize_role(role: Roles) -> RoleOut:
    links = list(role.permission_links or [])
    perms = [link.permission for link in links if link.permission is not None]
    perms.sort(key=lambda p: (p.section or "", p.label or "", p.id))
    return RoleOut(
        id=role.id,
        name=role.name,
        description=role.description,
        permission_ids=[p.id for p in perms],
        permissions=[PermissionOut.model_validate(p) for p in perms],
        created_at=role.created_at,
        updated_at=getattr(role, "updated_at", None),
    )


def _set_role_permissions(db: Session, role: Roles, permission_ids: list[int]) -> None:
    unique_ids = sorted({int(x) for x in permission_ids if int(x) > 0})
    if unique_ids:
        found = db.query(Permissions).filter(Permissions.id.in_(unique_ids)).all()
        if len(found) != len(unique_ids):
            raise HTTPException(status_code=400, detail="Uno o más permission_id no existen")
    db.query(RolePermissions).filter(RolePermissions.role_id == role.id).delete()
    for permission_id in unique_ids:
        db.add(RolePermissions(role_id=role.id, permission_id=permission_id))


@router.get("/roles/", response_model=list[RoleOut])
def list_roles(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[RoleOut]:
    rows = (
        db.query(Roles)
        .options(joinedload(Roles.permission_links).joinedload(RolePermissions.permission))
        .order_by(Roles.name.asc(), Roles.id.asc())
        .all()
    )
    return [_serialize_role(row) for row in rows]


@router.get("/roles/{role_id}", response_model=RoleOut)
def get_role(
    role_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> RoleOut:
    role = (
        db.query(Roles)
        .options(joinedload(Roles.permission_links).joinedload(RolePermissions.permission))
        .filter(Roles.id == role_id)
        .first()
    )
    if role is None:
        raise HTTPException(status_code=404, detail="Rol no encontrado")
    return _serialize_role(role)


@router.post("/roles/", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
def create_role(
    payload: RoleCreate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> RoleOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="El nombre es obligatorio")
    exists = db.query(Roles).filter(Roles.name == name).first()
    if exists:
        raise HTTPException(status_code=400, detail="Ya existe un rol con ese nombre")
    role = Roles(name=name, description=(payload.description or "").strip() or None)
    db.add(role)
    db.flush()
    _set_role_permissions(db, role, payload.permission_ids or [])
    db.commit()
    role = (
        db.query(Roles)
        .options(joinedload(Roles.permission_links).joinedload(RolePermissions.permission))
        .filter(Roles.id == role.id)
        .first()
    )
    return _serialize_role(role)


@router.put("/roles/{role_id}", response_model=RoleOut)
def update_role(
    role_id: int,
    payload: RoleUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> RoleOut:
    role = db.query(Roles).filter(Roles.id == role_id).first()
    if role is None:
        raise HTTPException(status_code=404, detail="Rol no encontrado")
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        name = str(data["name"]).strip()
        if not name:
            raise HTTPException(status_code=400, detail="El nombre es obligatorio")
        exists = db.query(Roles).filter(Roles.name == name, Roles.id != role_id).first()
        if exists:
            raise HTTPException(status_code=400, detail="Ya existe un rol con ese nombre")
        role.name = name
    if "description" in data:
        role.description = (str(data["description"] or "").strip() or None)
    if "permission_ids" in data and data["permission_ids"] is not None:
        _set_role_permissions(db, role, list(data["permission_ids"] or []))
    db.add(role)
    db.commit()
    role = (
        db.query(Roles)
        .options(joinedload(Roles.permission_links).joinedload(RolePermissions.permission))
        .filter(Roles.id == role_id)
        .first()
    )
    return _serialize_role(role)


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(
    role_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> None:
    role = db.query(Roles).filter(Roles.id == role_id).first()
    if role is None:
        raise HTTPException(status_code=404, detail="Rol no encontrado")
    users_count = db.query(Users).filter(Users.role_id == role_id).count()
    if users_count:
        raise HTTPException(
            status_code=400,
            detail="No se puede eliminar: hay usuarios asignados a este rol",
        )
    db.delete(role)
    db.commit()


@router.get("/permissions/", response_model=list[PermissionOut])
def list_permissions(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[PermissionOut]:
    rows = (
        db.query(Permissions)
        .order_by(Permissions.section.asc(), Permissions.label.asc(), Permissions.id.asc())
        .all()
    )
    return [PermissionOut.model_validate(row) for row in rows]


@router.post("/permissions/", response_model=PermissionOut, status_code=status.HTTP_201_CREATED)
def create_permission(
    payload: PermissionCreate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> PermissionOut:
    code = payload.code.strip().lower().replace(" ", "_")
    if not code:
        raise HTTPException(status_code=400, detail="El código es obligatorio")
    exists = db.query(Permissions).filter(Permissions.code == code).first()
    if exists:
        raise HTTPException(status_code=400, detail="Ya existe un permiso con ese código")
    row = Permissions(
        code=code,
        label=payload.label.strip(),
        section=(payload.section or "").strip() or code,
        description=(payload.description or "").strip(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return PermissionOut.model_validate(row)


@router.put("/permissions/{permission_id}", response_model=PermissionOut)
def update_permission(
    permission_id: int,
    payload: PermissionUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> PermissionOut:
    row = db.query(Permissions).filter(Permissions.id == permission_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Permiso no encontrado")
    data = payload.model_dump(exclude_unset=True)
    if "code" in data and data["code"] is not None:
        code = str(data["code"]).strip().lower().replace(" ", "_")
        exists = (
            db.query(Permissions)
            .filter(Permissions.code == code, Permissions.id != permission_id)
            .first()
        )
        if exists:
            raise HTTPException(status_code=400, detail="Ya existe un permiso con ese código")
        row.code = code
    if "label" in data and data["label"] is not None:
        row.label = str(data["label"]).strip()
    if "section" in data and data["section"] is not None:
        row.section = str(data["section"]).strip()
    if "description" in data and data["description"] is not None:
        row.description = str(data["description"]).strip()
    db.add(row)
    db.commit()
    db.refresh(row)
    return PermissionOut.model_validate(row)


@router.delete("/permissions/{permission_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_permission(
    permission_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> None:
    row = db.query(Permissions).filter(Permissions.id == permission_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Permiso no encontrado")
    db.query(RolePermissions).filter(RolePermissions.permission_id == permission_id).delete()
    db.delete(row)
    db.commit()
