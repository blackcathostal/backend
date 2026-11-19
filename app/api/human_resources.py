"""Human resources monthly amounts by Remuneraciones chart accounts."""

from __future__ import annotations

import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.accounting_accounts import AccountingAccounts
from app.models.hr_period_amounts import HrPeriodAmounts
from app.models.users import Users
from app.schemas.human_resources import (
    HrPeriodAmountLine,
    HrPeriodOut,
    HrPeriodUpdate,
)

router = APIRouter(prefix="/human-resources", tags=["human-resources"])

_REMUNERACIONES = "remuneraciones"


def _normalize_period(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        today = date.today()
        return f"{today.month:02d}-{today.year}"
    match = re.fullmatch(r"(\d{1,2})[-/](\d{4})", text)
    if match:
        month, year = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return f"{month:02d}-{year}"
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})", text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return f"{month:02d}-{year}"
    raise HTTPException(status_code=400, detail="Periodo inválido (usa MM-YYYY)")


def _load_remuneracion_accounts(db: Session) -> list[AccountingAccounts]:
    rows = (
        db.query(AccountingAccounts)
        .filter(
            AccountingAccounts.is_active.is_(True),
            AccountingAccounts.account_type == "gasto",
        )
        .order_by(
            AccountingAccounts.sort_order.asc(),
            AccountingAccounts.code.asc(),
            AccountingAccounts.id.asc(),
        )
        .all()
    )
    return [
        row
        for row in rows
        if str(row.category or "").strip().lower() == _REMUNERACIONES
    ]


def _period_payload(db: Session, period_key: str) -> HrPeriodOut:
    accounts = _load_remuneracion_accounts(db)
    if not accounts:
        return HrPeriodOut(period=period_key, lines=[])
    amounts = {
        int(row.accounting_account_id): int(row.amount or 0)
        for row in db.query(HrPeriodAmounts)
        .filter(HrPeriodAmounts.period == period_key)
        .all()
    }
    lines = [
        HrPeriodAmountLine(
            accounting_account_id=int(account.id),
            code=str(account.code or ""),
            name=str(account.name or ""),
            amount=int(amounts.get(int(account.id), 0)),
        )
        for account in accounts
    ]
    return HrPeriodOut(period=period_key, lines=lines)


@router.get("", response_model=HrPeriodOut, include_in_schema=False)
@router.get("/", response_model=HrPeriodOut)
def get_hr_period(
    period: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> HrPeriodOut:
    return _period_payload(db, _normalize_period(period))


@router.put("", response_model=HrPeriodOut, include_in_schema=False)
@router.put("/", response_model=HrPeriodOut)
def update_hr_period(
    payload: HrPeriodUpdate,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> HrPeriodOut:
    period_key = _normalize_period(payload.period)
    accounts = _load_remuneracion_accounts(db)
    allowed = {int(account.id) for account in accounts}
    if not allowed:
        raise HTTPException(
            status_code=400,
            detail="No hay cuentas activas de Remuneraciones en el plan de cuentas",
        )

    existing = {
        int(row.accounting_account_id): row
        for row in db.query(HrPeriodAmounts)
        .filter(HrPeriodAmounts.period == period_key)
        .all()
    }

    seen: set[int] = set()
    for line in payload.lines or []:
        account_id = int(line.accounting_account_id)
        if account_id not in allowed or account_id in seen:
            continue
        seen.add(account_id)
        amount = max(0, int(line.amount or 0))
        row = existing.get(account_id)
        if row is None:
            row = HrPeriodAmounts(
                period=period_key,
                accounting_account_id=account_id,
                amount=amount,
            )
            db.add(row)
            existing[account_id] = row
        else:
            row.amount = amount

    db.commit()
    return _period_payload(db, period_key)