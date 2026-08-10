"""Import operadores.xlsx into production Black Cat API contacts."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import httpx
import pandas as pd

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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--api",
        default="https://api.blackcathostal.com/api",
    )
    parser.add_argument("--email", default="admin@blackcathostal.com")
    parser.add_argument("--password", default="admin123")
    parser.add_argument(
        "--file",
        type=Path,
        default=Path(r"C:\Users\jesus\Desktop\proyecto_blackcat\operadores.xlsx"),
    )
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--group", default="Agencias")
    args = parser.parse_args()

    rows = load_rows(args.file, only_vigente=not args.all)
    print(f"API: {args.api}")
    print(f"Unique emails: {len(rows)}")

    with httpx.Client(base_url=args.api, timeout=60.0, follow_redirects=True) as client:
        login = client.post(
            "/auth/login",
            json={"email": args.email, "password": args.password},
        )
        login.raise_for_status()
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        groups = client.get("/contact-groups/", headers=headers)
        groups.raise_for_status()
        group_id = None
        for g in groups.json():
            if g.get("name") == args.group:
                group_id = g["id"]
                break
        if group_id is None:
            created = client.post(
                "/contact-groups/",
                headers=headers,
                json={
                    "name": args.group,
                    "description": "Agencias de viaje y operadores (SERNATUR)",
                    "is_active": True,
                },
            )
            created.raise_for_status()
            group_id = created.json()["id"]
            print(f"Created group {args.group} id={group_id}")
        else:
            print(f"Using group {args.group} id={group_id}")

        existing = client.get("/contacts/", headers=headers)
        existing.raise_for_status()
        existing_emails = {c["email"].lower() for c in existing.json()}
        print(f"Existing contacts on prod: {len(existing_emails)}")

        created_n = 0
        skipped = 0
        failed = 0
        for i, (full_name, email) in enumerate(rows, start=1):
            if email in existing_emails:
                skipped += 1
                continue
            resp = client.post(
                "/contacts/",
                headers=headers,
                json={
                    "full_name": full_name,
                    "email": email,
                    "is_active": True,
                    "group_id": group_id,
                },
            )
            if resp.status_code in (200, 201):
                created_n += 1
                existing_emails.add(email)
            elif resp.status_code == 400 and "already exists" in resp.text.lower():
                skipped += 1
                existing_emails.add(email)
            else:
                failed += 1
                if failed <= 10:
                    print(f"FAIL {email}: {resp.status_code} {resp.text[:200]}")
            if i % 100 == 0:
                print(f"  progress {i}/{len(rows)} created={created_n} skipped={skipped} failed={failed}")

        final = client.get("/contacts/", headers=headers)
        final.raise_for_status()
        print(f"Created: {created_n}")
        print(f"Skipped: {skipped}")
        print(f"Failed: {failed}")
        print(f"Total contacts on prod now: {len(final.json())}")


if __name__ == "__main__":
    main()
