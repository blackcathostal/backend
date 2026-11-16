import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.accounting_accounts import AccountingAccounts
from app.models.users import Users
from app.schemas.accounting_accounts import (
    AccountingAccountsCreate,
    AccountingAccountsImportResult,
    AccountingAccountsOut,
    AccountingAccountsUpdate,
)
from app.services.facturacion_client import FacturacionClient, FacturacionError

router = APIRouter(prefix="/accounting-accounts", tags=["accounting-accounts"])

COA_FILE = Path(__file__).resolve().parents[1] / "data" / "chile_hostel_coa.json"
VALID_TYPES = {"activo", "pasivo", "patrimonio", "ingreso", "gasto"}
VALID_NATURES = {"deudora", "acreedora"}


def _load_coa_catalog() -> list[dict]:
    try:
        raw = json.loads(COA_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail="Catálogo de cuentas contables no encontrado en el servidor",
        ) from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail="Catálogo de cuentas contables inválido",
        ) from exc

    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=500, detail="Catálogo de cuentas contables vacío")

    accounts: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        account_type = str(item.get("account_type") or "").strip().lower()
        nature = str(item.get("nature") or "deudora").strip().lower()
        if not code or not name:
            continue
        if account_type not in VALID_TYPES or nature not in VALID_NATURES:
            continue
        accounts.append(
            {
                "code": code,
                "name": name,
                "account_type": account_type,
                "nature": nature,
            }
        )
    if not accounts:
        raise HTTPException(status_code=500, detail="Catálogo de cuentas contables sin filas válidas")
    return accounts


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


@router.post("/import-facturacion", response_model=AccountingAccountsImportResult)
def import_accounting_accounts_from_facturacion(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> AccountingAccountsImportResult:
    client = FacturacionClient()
    try:
        client.login()
    except FacturacionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    catalog = _load_coa_catalog()
    existing = {
        row.code: row
        for row in db.query(AccountingAccounts).all()
    }

    created = 0
    updated = 0
    for item in catalog:
        code = item["code"]
        row = existing.get(code)
        if row is None:
            account = AccountingAccounts(
                code=code,
                name=item["name"],
                account_type=item["account_type"],
                nature=item["nature"],
                description=None,
                is_active=True,
            )
            db.add(account)
            existing[code] = account
            created += 1
            continue

        changed = False
        if row.name != item["name"]:
            row.name = item["name"]
            changed = True
        if row.account_type != item["account_type"]:
            row.account_type = item["account_type"]
            changed = True
        if row.nature != item["nature"]:
            row.nature = item["nature"]
            changed = True
        if not row.is_active:
            row.is_active = True
            changed = True
        if changed:
            updated += 1

    db.commit()
    return AccountingAccountsImportResult(
        created=created,
        updated=updated,
        total=len(catalog),
    )


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
