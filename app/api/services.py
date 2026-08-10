from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.services import Services
from app.models.users import Users
from app.schemas.services import ServicesCreate, ServicesOut, ServicesUpdate

router = APIRouter(prefix="/services", tags=["services"])


@router.get("/", response_model=list[ServicesOut])
def list_services(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[Services]:
    return db.query(Services).order_by(Services.id.desc()).all()


@router.get("/{service_id}", response_model=ServicesOut)
def get_service(
    service_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Services:
    service = db.query(Services).filter(Services.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    return service


@router.post("/", response_model=ServicesOut, status_code=status.HTTP_201_CREATED)
def create_service(
    payload: ServicesCreate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Services:
    service = Services(
        name=payload.name.strip(),
        category=payload.category.strip(),
        price=payload.price.strip(),
        status=(payload.status or "Activo").strip(),
        description=payload.description.strip() if payload.description else None,
    )
    db.add(service)
    db.commit()
    db.refresh(service)
    return service


@router.put("/{service_id}", response_model=ServicesOut)
def update_service(
    service_id: int,
    payload: ServicesUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Services:
    service = db.query(Services).filter(Services.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    data = payload.model_dump(exclude_unset=True)
    for key in ("name", "category", "price", "status"):
        if key in data and data[key] is not None:
            data[key] = str(data[key]).strip()
    if "description" in data and data["description"] is not None:
        data["description"] = str(data["description"]).strip() or None

    for key, value in data.items():
        setattr(service, key, value)

    db.commit()
    db.refresh(service)
    return service


@router.delete("/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_service(
    service_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> None:
    service = db.query(Services).filter(Services.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    db.delete(service)
    db.commit()
