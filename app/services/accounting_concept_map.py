"""Resolve Cobros concept labels to accounting accounts from the DB catalog."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from sqlalchemy.orm import Session

from app.models.accounting_accounts import AccountingAccounts

CATCH_ALL_LABEL = "*"
# Short tokens only match exactly (avoid "a" ∈ "estacionamiento", etc.).
_MIN_PARTIAL_TOKEN_LEN = 4

COA_FILE = Path(__file__).resolve().parents[1] / "data" / "blackcat_eerr_coa.json"


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


def load_income_accounts(db: Session) -> list[AccountingAccounts]:
    rows = (
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
    ingresos = [row for row in rows if (row.category or "").strip().lower() == "ingresos"]
    others = [row for row in rows if (row.category or "").strip().lower() != "ingresos"]
    return ingresos + others if ingresos else rows


def load_expense_accounts(db: Session) -> list[AccountingAccounts]:
    """Active gasto accounts for EERR egresos (Remuneraciones, Materiales, …)."""
    return (
        db.query(AccountingAccounts)
        .filter(
            AccountingAccounts.is_active.is_(True),
            AccountingAccounts.account_type == "gasto",
        )
        .order_by(
            AccountingAccounts.category.asc(),
            AccountingAccounts.sort_order.asc(),
            AccountingAccounts.code.asc(),
        )
        .all()
    )


def load_fixed_asset_accounts(db: Session) -> list[AccountingAccounts]:
    """Active fixed-asset (Activo Fijo) accounts for the EERR purchases section."""
    rows = (
        db.query(AccountingAccounts)
        .filter(
            AccountingAccounts.is_active.is_(True),
            AccountingAccounts.account_type == "activo",
        )
        .order_by(
            AccountingAccounts.sort_order.asc(),
            AccountingAccounts.code.asc(),
        )
        .all()
    )
    return [
        row
        for row in rows
        if str(row.category or "").strip().lower().replace("ó", "o") == "activo fijo"
    ]


def _load_coa_rows() -> list[dict]:
    try:
        raw = json.loads(COA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    rows: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        if not code or not name:
            continue
        rows.append(item)
    return rows


def ensure_concept_labels_from_coa(db: Session) -> int:
    """
    Sync chart rows from blackcat_eerr_coa.json (ingreso + gasto + …):
    create missing accounts and refresh name/labels/sort for codes present in the file.
    Returns number of accounts created or updated.
    """
    catalog = _load_coa_rows()
    if not catalog:
        return 0

    existing = {
        str(row.code or "").strip(): row
        for row in db.query(AccountingAccounts).all()
        if str(row.code or "").strip()
    }
    changed = 0
    for item in catalog:
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        category = str(item.get("category") or "").strip() or "Ingresos"
        account_type = str(item.get("account_type") or "ingreso").strip().lower()
        nature = str(item.get("nature") or "acreedora").strip().lower()
        try:
            sort_order = int(item.get("sort_order") or 0)
        except (TypeError, ValueError):
            sort_order = 0
        labels_json = serialize_concept_labels(parse_concept_labels(item.get("concept_labels")))
        is_active = item.get("is_active", True)
        if isinstance(is_active, str):
            is_active = is_active.strip().lower() not in {"0", "false", "no", "off"}
        else:
            is_active = bool(is_active)
        row = existing.get(code)
        if row is None:
            if not is_active:
                continue
            account = AccountingAccounts(
                code=code,
                name=name,
                category=category,
                account_type=account_type,
                nature=nature,
                sort_order=sort_order,
                description=None,
                concept_labels=labels_json,
                is_active=True,
            )
            db.add(account)
            existing[code] = account
            changed += 1
            continue

        row_changed = False
        if (row.name or "") != name:
            row.name = name
            row_changed = True
        if (row.category or "") != category:
            row.category = category
            row_changed = True
        if (row.account_type or "") != account_type:
            row.account_type = account_type
            row_changed = True
        if (row.nature or "") != nature:
            row.nature = nature
            row_changed = True
        if int(row.sort_order or 0) != sort_order:
            row.sort_order = sort_order
            row_changed = True
        if (row.concept_labels or "[]") != labels_json:
            row.concept_labels = labels_json
            row_changed = True
        if bool(row.is_active) != is_active:
            row.is_active = is_active
            row_changed = True
        if row_changed:
            changed += 1

    if changed:
        db.commit()
    return changed


def catch_all_account(
    accounts: list[AccountingAccounts] | None = None,
    *,
    db: Session | None = None,
) -> AccountingAccounts | None:
    """Only an account that explicitly lists '*' is the catch-all. Never invent one."""
    rows = accounts if accounts is not None else (load_income_accounts(db) if db else [])
    for row in rows:
        labels = {_norm(label) for label in parse_concept_labels(getattr(row, "concept_labels", None))}
        if CATCH_ALL_LABEL in labels:
            return row
    return None


def default_income_account(
    accounts: list[AccountingAccounts] | None = None,
    *,
    db: Session | None = None,
) -> AccountingAccounts | None:
    rows = accounts if accounts is not None else (load_income_accounts(db) if db else [])
    return rows[0] if rows else None


def lodging_income_account(
    accounts: list[AccountingAccounts] | None = None,
    *,
    db: Session | None = None,
) -> AccountingAccounts | None:
    """
    Default account for issued invoices/boletas without Cobros concepts.
    Resolves via catalog concept_labels (Alojamiento / Hospedaje), never a hardcoded code.
    """
    rows = accounts if accounts is not None else (load_income_accounts(db) if db else [])
    if not rows:
        return None
    for label in ("Alojamiento", "Hospedaje"):
        matched = match_account_for_concept(label, rows)
        if matched is None:
            continue
        labels = {
            _norm(item)
            for item in parse_concept_labels(getattr(matched, "concept_labels", None))
        }
        lodging_tokens = {_norm("Alojamiento"), _norm("Hospedaje"), "aloj", "hosped"}
        if labels & lodging_tokens:
            return matched
        if any(
            _token_matches(_norm(label), token)
            for token in labels
            if token and token != CATCH_ALL_LABEL
        ):
            return matched
    return default_income_account(rows)


def _token_matches(needle: str, token: str) -> bool:
    if not needle or not token or token == CATCH_ALL_LABEL:
        return False
    if token == needle:
        return True
    if len(token) < _MIN_PARTIAL_TOKEN_LEN and len(needle) < _MIN_PARTIAL_TOKEN_LEN:
        return False
    if len(token) < _MIN_PARTIAL_TOKEN_LEN:
        # Short catalog token: only as whole word / exact already handled.
        return token == needle or re.search(rf"(?:^|[^a-z0-9]){re.escape(token)}(?:[^a-z0-9]|$)", needle) is not None
    if len(needle) < _MIN_PARTIAL_TOKEN_LEN:
        return False
    return token in needle or needle in token


def match_account_for_concept(
    concept: object,
    accounts: list[AccountingAccounts],
    *,
    allow_catch_all: bool = True,
) -> AccountingAccounts | None:
    """Match a Cobros concept column/header via concept_labels only (not account name)."""
    needle = _norm(concept)
    if not needle:
        return catch_all_account(accounts) if allow_catch_all else None

    exact: AccountingAccounts | None = None
    partial: tuple[int, AccountingAccounts] | None = None
    for account in accounts:
        for label in parse_concept_labels(getattr(account, "concept_labels", None)):
            token = _norm(label)
            if not token or token == CATCH_ALL_LABEL:
                continue
            if token == needle:
                exact = account
                break
            if _token_matches(needle, token):
                score = len(token)
                if partial is None or score > partial[0]:
                    partial = (score, account)
        if exact is not None:
            return exact
    if partial is not None:
        return partial[1]
    return catch_all_account(accounts) if allow_catch_all else None


def account_code_for_concept(
    concept: object,
    accounts: list[AccountingAccounts],
    *,
    allow_catch_all: bool = True,
) -> str | None:
    account = match_account_for_concept(
        concept, accounts, allow_catch_all=allow_catch_all
    )
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
    has_any_labels = False
    for account in accounts:
        labels = parse_concept_labels(getattr(account, "concept_labels", None))
        if labels:
            has_any_labels = True
        for label in labels:
            token = _norm(label)
            if not token or token == CATCH_ALL_LABEL:
                continue
            if _token_matches(needle, token):
                return True
    # If catalog has no labels yet, treat non-empty headers as candidate concepts
    # (caller still excludes structural Cobros columns).
    return not has_any_labels


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
        matched = match_account_for_concept(best_name, accounts)
        if matched is not None:
            return matched
    return default_income_account(accounts)
