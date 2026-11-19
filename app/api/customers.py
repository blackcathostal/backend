from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.customers import Customers
from app.models.users import Users
from app.schemas.customers import CustomerOut

router = APIRouter(prefix="/customers", tags=["customers"])


@router.get("/", response_model=list[CustomerOut])
def list_customers(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[CustomerOut]:
    rows = (
        db.query(Customers)
        .order_by(Customers.company_name.asc(), Customers.identification_number.asc(), Customers.id.asc())
        .all()
    )
    return [CustomerOut.model_validate(row) for row in rows]
