from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session, joinedload

from app.core.database import get_db
from app.core.security import create_access_token, get_current_user, verify_password
from app.models.permissions import RolePermissions
from app.models.users import Users
from app.schemas.auth import LoginRequest, TokenResponse, UsersOut

router = APIRouter(prefix="/auth", tags=["auth"])


def _permission_codes_for_user(db: Session, user: Users) -> list[str]:
    if not user.role_id:
        return []
    rows = (
        db.query(RolePermissions)
        .options(joinedload(RolePermissions.permission))
        .filter(RolePermissions.role_id == user.role_id)
        .all()
    )
    codes = sorted(
        {
            str(link.permission.code)
            for link in rows
            if link.permission is not None and str(link.permission.code or "").strip()
        }
    )
    return codes


def _serialize_me(db: Session, user: Users) -> UsersOut:
    payload = {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "is_active": user.is_active,
        "role": user.role,
        "permission_codes": _permission_codes_for_user(db, user),
    }
    return UsersOut.model_validate(payload)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.query(Users).filter(Users.email == payload.email).first()
    if not user or not verify_password(payload.password, user.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    return TokenResponse(access_token=create_access_token(user.email))


@router.post("/login-form", response_model=TokenResponse, include_in_schema=False)
def login_form(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> TokenResponse:
    user = db.query(Users).filter(Users.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return TokenResponse(access_token=create_access_token(user.email))


@router.get("/me", response_model=UsersOut)
def me(current_user: Users = Depends(get_current_user), db: Session = Depends(get_db)) -> UsersOut:
    user = (
        db.query(Users)
        .options(joinedload(Users.role))
        .filter(Users.id == current_user.id)
        .first()
    )
    return _serialize_me(db, user or current_user)
