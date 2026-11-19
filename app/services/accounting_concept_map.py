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


def ensure_concept_labels_from_coa(db: Session) -> int:
    """
    Backfill empty concept_labels from blackcat_eerr_coa.json.
    Returns number of accounts updated.
    """
    try:
        raw = json.loads(COA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return 0
    if not isinstance(raw, list):
        return 0
    by_code = {
        str(item.get("code") or "").strip(): parse_concept_labels(item.get("concept_labels"))
        for item in raw
        if isinstance(item, dict) and str(item.get("code") or "").strip()
    }
    updated = 0
    for row in db.query(AccountingAccounts).all():
        code = str(row.code or "").strip()
        existing = parse_concept_labels(getattr(row, "concept_labels", None))
        if existing:
            continue
        labels = by_code.get(code) or []
        if not labels:
            continue
        row.concept_labels = serialize_concept_labels(labels)
        updated += 1
    if updated:
        db.commit()
    return updated


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
) -> AccountingAccounts | None:
    """Match a Cobros concept column/header via concept_labels only (not account name)."""
    needle = _norm(concept)
    if not needle:
        return catch_all_account(accounts)

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
    return catch_all_account(accounts)


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
