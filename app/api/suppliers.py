from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.suppliers import Suppliers
from app.models.users import Users
from app.schemas.suppliers import SupplierOut

router = APIRouter(prefix="/suppliers", tags=["suppliers"])


@router.get("/", response_model=list[SupplierOut])
def list_suppliers(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[SupplierOut]:
    rows = (
        db.query(Suppliers)
        .order_by(Suppliers.company_name.asc(), Suppliers.identification_number.asc(), Suppliers.id.asc())
        .all()
    )
    return [SupplierOut.model_validate(row) for row in rows]
