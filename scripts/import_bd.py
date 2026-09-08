"""Import bd.xlsx (Corporativo + Productoras) into contacts.

Usage:
  python scripts/import_bd.py
  python scripts/import_bd.py --file C:\\Users\\jesus\\Downloads\\bd.xlsx
  python scripts/import_bd_api.py   # via production API (recommended when tunnel auth fails)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.models.contact_groups import ContactGroups  # noqa: E402
from app.models.contacts import Contacts  # noqa: E402

EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.I)
INVALID_MARKERS = {"", "nan", "none", "dato protegido", "n/a", "na", "-", "null"}


def extract_emails(raw: object) -> list[str]:
    if raw is None:
        return []
    text = str(raw).strip()
    if text.lower() in INVALID_MARKERS:
        return []
    # Strip junk after @domain
    text = re.sub(r"([\w.\-+]+@[\w.\-]+\.\w{2,})[^\w@.\-+].*$", r"\1", text)
    parts = re.split(r"[;,\s/|]+", text)
    out: list[str] = []
    for part in parts:
        email = part.strip().strip("<>()[]\"'")
        if EMAIL_RE.match(email):
            out.append(email.lower())
    return out


def pick_name(*candidates: object, fallback: str = "Sin nombre") -> str:
    for raw in candidates:
        value = str(raw or "").strip()
        if value and value.lower() not in INVALID_MARKERS:
            return value[:160]
    return fallback[:160]


def load_corporativo(path: Path) -> list[tuple[str, str]]:
    df = pd.read_excel(path, sheet_name="Corporativo", dtype=str)
    mail_col = next(c for c in df.columns if "mail" in c.lower())
    seen: set[str] = set()
    rows: list[tuple[str, str]] = []
    for _, row in df.iterrows():
        emails = extract_emails(row.get(mail_col))
        if not emails:
            continue
        name = pick_name(row.get("CONTACTO"), row.get("EMPRESA"), row.get("RAZON SOCIAL"))
        for email in emails:
            if email in seen:
                continue
            seen.add(email)
            rows.append((name, email))
    return rows


def load_productoras(path: Path) -> list[tuple[str, str]]:
    df = pd.read_excel(path, sheet_name="Productoras", dtype=str, header=1)
    correo_col = next(
        (c for c in df.columns if "correo" in str(c).lower() or "mail" in str(c).lower()),
        None,
    )
    if correo_col is None:
        return []
    seen: set[str] = set()
    rows: list[tuple[str, str]] = []
    for _, row in df.iterrows():
        emails = extract_emails(row.get(correo_col))
        if not emails:
            continue
        name = pick_name(row.get("Contacto"), row.get("Productora"))
        for email in emails:
            if email in seen:
                continue
            seen.add(email)
            rows.append((name, email))
    return rows


def ensure_group(db: Session, name: str, description: str) -> ContactGroups:
    group = db.scalar(select(ContactGroups).where(ContactGroups.name == name))
    if group:
        return group
    group = ContactGroups(name=name, description=description, is_active=True)
    db.add(group)
    db.flush()
    return group


def import_rows(
    db: Session,
    group: ContactGroups,
    rows: list[tuple[str, str]],
    existing_emails: set[str],
) -> tuple[int, int]:
    created = 0
    skipped = 0
    for full_name, email in rows:
        if email in existing_emails:
            skipped += 1
            continue
        db.add(
            Contacts(
                full_name=full_name,
                email=email,
                is_active=True,
                group_id=group.id,
            )
        )
        existing_emails.add(email)
        created += 1
        if created % 200 == 0:
            db.commit()
    return created, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="Import bd.xlsx Corporativo + Productoras")
    parser.add_argument(
        "--file",
        type=Path,
        default=Path(r"C:\Users\jesus\Downloads\bd.xlsx"),
    )
    parser.add_argument(
        "--database-url",
        default="mysql+pymysql://root@127.0.0.1:3306/bc",
    )
    args = parser.parse_args()

    if not args.file.exists():
        raise SystemExit(f"File not found: {args.file}")

    corporativo_rows = load_corporativo(args.file)
    productoras_rows = load_productoras(args.file)
    print(f"File: {args.file}")
    print(f"Corporativo: {len(corporativo_rows)} unique emails")
    print(f"Productoras: {len(productoras_rows)} unique emails")

    engine = create_engine(args.database_url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        group_corp = ensure_group(
            db,
            "Corporativo",
            "Contactos corporativos (bd.xlsx)",
        )
        group_prod = ensure_group(
            db,
            "Productoras",
            "Productoras y agencias (bd.xlsx)",
        )
        db.commit()

        existing_emails = {e.lower() for (e,) in db.execute(select(Contacts.email)).all()}

        c1, s1 = import_rows(db, group_corp, corporativo_rows, existing_emails)
        db.commit()
        c2, s2 = import_rows(db, group_prod, productoras_rows, existing_emails)
        db.commit()

        in_corp = db.query(Contacts).filter(Contacts.group_id == group_corp.id).count()
        in_prod = db.query(Contacts).filter(Contacts.group_id == group_prod.id).count()
        total = db.query(Contacts).count()

        print(f"\nCorporativo — created: {c1}, skipped: {s1}, in group: {in_corp}")
        print(f"Productoras — created: {c2}, skipped: {s2}, in group: {in_prod}")
        print(f"Total contacts in DB: {total}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
