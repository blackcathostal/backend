from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.accounting_accounts import AccountingAccounts
from app.models.users import Users
from app.schemas.accounting_accounts import (
    AccountingAccountsCreate,
    AccountingAccountsOut,
    AccountingAccountsUpdate,
)

router = APIRouter(prefix="/accounting-accounts", tags=["accounting-accounts"])


@router.get("/", response_model=list[AccountingAccountsOut])
def list_accounting_accounts(
    active_only: bool = False,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[AccountingAccounts]:
    query = db.query(AccountingAccounts)
    if active_only:
        query = query.filter(AccountingAccounts.is_active.is_(True))
    return query.order_by(AccountingAccounts.code.asc(), AccountingAccounts.id.asc()).all()


@router.get("/{account_id}", response_model=AccountingAccountsOut)
def get_accounting_account(
    account_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> AccountingAccounts:
    account = db.query(AccountingAccounts).filter(AccountingAccounts.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Cuenta contable no encontrada")
    return account


@router.post("/", response_model=AccountingAccountsOut, status_code=status.HTTP_201_CREATED)
def create_accounting_account(
    payload: AccountingAccountsCreate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> AccountingAccounts:
    code = payload.code.strip()
    exists = db.query(AccountingAccounts).filter(AccountingAccounts.code == code).first()
    if exists:
        raise HTTPException(status_code=400, detail="Ya existe una cuenta con ese código")

    account = AccountingAccounts(
        code=code,
        name=payload.name.strip(),
        account_type=payload.account_type,
        nature=payload.nature,
        description=(payload.description or "").strip() or None,
        is_active=payload.is_active,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.put("/{account_id}", response_model=AccountingAccountsOut)
def update_accounting_account(
    account_id: int,
    payload: AccountingAccountsUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> AccountingAccounts:
    account = db.query(AccountingAccounts).filter(AccountingAccounts.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Cuenta contable no encontrada")

    data = payload.model_dump(exclude_unset=True)
    if "code" in data and data["code"] is not None:
        code = str(data["code"]).strip()
        exists = (
            db.query(AccountingAccounts)
            .filter(AccountingAccounts.code == code, AccountingAccounts.id != account_id)
            .first()
        )
        if exists:
            raise HTTPException(status_code=400, detail="Ya existe una cuenta con ese código")
        data["code"] = code
    if "name" in data and data["name"] is not None:
        data["name"] = str(data["name"]).strip()
    if "description" in data and data["description"] is not None:
        cleaned = str(data["description"]).strip()
        data["description"] = cleaned or None

    for key, value in data.items():
        setattr(account, key, value)

    db.commit()
    db.refresh(account)
    return account


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_accounting_account(
    account_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> None:
    account = db.query(AccountingAccounts).filter(AccountingAccounts.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Cuenta contable no encontrada")
    db.delete(account)
    db.commit()
