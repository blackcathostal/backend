from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.users import Users
from app.schemas.terms import TermsOut, TermsUpdate
from app.services import terms as svc

router = APIRouter(prefix="/terms", tags=["terms"])


@router.get("/public/{locale}", response_model=TermsOut)
def public_terms(locale: str, db: Session = Depends(get_db)) -> TermsOut:
    try:
        row = svc.get_terms(db, locale)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Términos no encontrados") from exc
    return TermsOut(**svc.serialize(row))


@router.get("/", response_model=list[TermsOut])
def admin_list_terms(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[TermsOut]:
    return [TermsOut(**svc.serialize(row)) for row in svc.list_terms(db)]


@router.get("/{locale}", response_model=TermsOut)
def admin_get_terms(
    locale: str,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TermsOut:
    try:
        row = svc.get_terms(db, locale)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Términos no encontrados") from exc
    return TermsOut(**svc.serialize(row))


@router.put("/{locale}", response_model=TermsOut)
def admin_update_terms(
    locale: str,
    payload: TermsUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TermsOut:
    if locale not in {"es", "en", "pt"}:
        raise HTTPException(status_code=400, detail="Idioma no válido")
    row = svc.update_terms(
        db,
        locale,
        title=payload.title,
        intro=payload.intro,
        body_html=payload.body_html,
    )
    return TermsOut(**svc.serialize(row))
