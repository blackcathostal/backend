"""Resolve FNS Cobros concept labels to accounting accounts from the DB catalog."""

from __future__ import annotations

import json
import re
from typing import Iterable

from sqlalchemy.orm import Session

from app.models.accounting_accounts import AccountingAccounts

CATCH_ALL_LABEL = "*"


def _norm(text: object) -> str:
    value = str(text or "").strip().lower()
    for src, dst in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n")):
        value = value.replace(src, dst)
    return value


def parse_concept_labels(raw: object) -> list[str]:
    """Parse concept_labels from JSON list, CSV, or plain text."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            loaded = json.loads(text)
            if isinstance(loaded, list):
                return [str(item).strip() for item in loaded if str(item).strip()]
        except json.JSONDecodeError:
            pass
    return [part.strip() for part in re.split(r"[,;|]", text) if part.strip()]


def serialize_concept_labels(labels: Iterable[str]) -> str:
    cleaned = [str(item).strip() for item in labels if str(item).strip()]
    return json.dumps(cleaned, ensure_ascii=False)


def _label_tokens(account: AccountingAccounts) -> list[str]:
    labels = parse_concept_labels(getattr(account, "concept_labels", None))
    # Also allow matching the account name itself.
    name = str(account.name or "").strip()
    if name:
        labels.append(name)
    return labels


def load_income_accounts(db: Session) -> list[AccountingAccounts]:
    return (
        db.query(AccountingAccounts)
        .filter(
            AccountingAccounts.is_active.is_(True),
            AccountingAccounts.account_type == "ingreso",
        )
        .order_by(
            AccountingAccounts.sort_order.asc(),
            AccountingAccounts.code.asc(),
        )
        .all()
    )


def catch_all_account(
    accounts: list[AccountingAccounts] | None = None,
    *,
    db: Session | None = None,
) -> AccountingAccounts | None:
    rows = accounts if accounts is not None else (load_income_accounts(db) if db else [])
    for row in rows:
        labels = {_norm(label) for label in parse_concept_labels(getattr(row, "concept_labels", None))}
        if CATCH_ALL_LABEL in labels:
            return row
    return rows[-1] if rows else None


def default_income_account(
    accounts: list[AccountingAccounts] | None = None,
    *,
    db: Session | None = None,
) -> AccountingAccounts | None:
    rows = accounts if accounts is not None else (load_income_accounts(db) if db else [])
    return rows[0] if rows else None


def match_account_for_concept(
    concept: object,
    accounts: list[AccountingAccounts],
) -> AccountingAccounts | None:
    """Match an FNS concept column/header to an accounting account via concept_labels."""
    needle = _norm(concept)
    if not needle:
        return catch_all_account(accounts) or default_income_account(accounts)

    # Prefer exact label match, then substring either way (longest label wins).
    exact: AccountingAccounts | None = None
    partial: tuple[int, AccountingAccounts] | None = None
    for account in accounts:
        for label in _label_tokens(account):
            token = _norm(label)
            if not token or token == CATCH_ALL_LABEL:
                continue
            if token == needle:
                exact = account
                break
            if token in needle or needle in token:
                score = len(token)
                if partial is None or score > partial[0]:
                    partial = (score, account)
        if exact is not None:
            return exact
    if partial is not None:
        return partial[1]
    return catch_all_account(accounts) or default_income_account(accounts)


def account_code_for_concept(
    concept: object,
    accounts: list[AccountingAccounts],
) -> str | None:
    account = match_account_for_concept(concept, accounts)
    return str(account.code) if account is not None else None


def account_id_for_concept(
    concept: object,
    accounts: list[AccountingAccounts],
) -> int | None:
    account = match_account_for_concept(concept, accounts)
    return int(account.id) if account is not None else None


def is_concept_column(header: object, accounts: list[AccountingAccounts]) -> bool:
    """True when header matches any configured concept label (not catch-all)."""
    needle = _norm(header)
    if not needle:
        return False
    for account in accounts:
        for label in parse_concept_labels(getattr(account, "concept_labels", None)):
            token = _norm(label)
            if not token or token == CATCH_ALL_LABEL:
                continue
            if token == needle or token in needle or needle in token:
                return True
    return False


def pick_account_from_concepts(
    concepts: dict | None,
    accounts: list[AccountingAccounts],
) -> AccountingAccounts | None:
    """Pick account from the concept with the largest absolute amount."""
    rows = concepts if isinstance(concepts, dict) else {}
    best_name = ""
    best_amount = 0.0
    for name, amount in rows.items():
        value = abs(float(amount or 0))
        if value >= best_amount:
            best_amount = value
            best_name = str(name or "")
    if best_name:
        return match_account_for_concept(best_name, accounts)
    return default_income_account(accounts)
