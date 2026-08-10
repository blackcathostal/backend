"""Import operadores.xlsx into contacts (group Agencias).

Default: only Vigencia=Vigente with a valid email.
Usage:
  python scripts/import_operadores.py
  python scripts/import_operadores.py --all
  python scripts/import_operadores.py --file path/to/file.xlsx
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
    parts = re.split(r"[;,\s/|]+", text)
    out: list[str] = []
    for part in parts:
        email = part.strip().strip("<>()[]\"'")
        if EMAIL_RE.match(email):
            out.append(email.lower())
    return out


def pick_name(row: pd.Series) -> str:
    for key in ("Nombre Comercial", "Razon Social"):
        value = str(row.get(key) or "").strip()
        if value and value.lower() not in INVALID_MARKERS:
            return value[:160]
    return "Sin nombre"


def load_rows(path: Path, *, only_vigente: bool) -> list[tuple[str, str]]:
    df = pd.read_excel(path, dtype=str)
    seen: set[str] = set()
    rows: list[tuple[str, str]] = []
    for _, row in df.iterrows():
        if only_vigente and str(row.get("Vigencia") or "").strip() != "Vigente":
            continue
        emails = extract_emails(row.get("EMAIL"))
        if not emails:
            continue
        name = pick_name(row)
        for email in emails:
            if email in seen:
                continue
            seen.add(email)
            rows.append((name, email))
    return rows


def ensure_group(db: Session, name: str = "Agencias") -> ContactGroups:
    group = db.scalar(select(ContactGroups).where(ContactGroups.name == name))
    if group:
        return group
    group = ContactGroups(
        name=name,
        description="Agencias de viaje y operadores (SERNATUR)",
        is_active=True,
    )
    db.add(group)
    db.flush()
    return group


def main() -> None:
    parser = argparse.ArgumentParser(description="Import operadores into contacts")
    parser.add_argument(
        "--file",
        type=Path,
        default=Path(r"C:\Users\jesus\Desktop\proyecto_blackcat\operadores.xlsx"),
    )
    parser.add_argument(
        "--database-url",
        default="mysql+pymysql://root@127.0.0.1:3306/bc",
        help="SQLAlchemy URL (default: local Laragon bc DB)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Import Vigente + No Vigente (still requires valid email)",
    )
    parser.add_argument(
        "--group",
        default="Agencias",
        help="Contact group name",
    )
    args = parser.parse_args()

    if not args.file.exists():
        raise SystemExit(f"File not found: {args.file}")

    only_vigente = not args.all
    rows = load_rows(args.file, only_vigente=only_vigente)
    print(f"File: {args.file}")
    print(f"Filter: {'Vigente only' if only_vigente else 'all with valid email'}")
    print(f"Unique emails to import: {len(rows)}")

    engine = create_engine(args.database_url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        group = ensure_group(db, args.group)
        existing_emails = {
            e.lower()
            for (e,) in db.execute(select(Contacts.email)).all()
        }

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
            if created % 500 == 0:
                db.commit()
                print(f"  … {created} created")

        db.commit()
        total = db.query(Contacts).count()
        in_group = db.query(Contacts).filter(Contacts.group_id == group.id).count()
        print(f"Created: {created}")
        print(f"Skipped (already existed): {skipped}")
        print(f"Group '{group.name}' (id={group.id}): {in_group} contacts")
        print(f"Total contacts in DB: {total}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
