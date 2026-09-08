"""Import bd.xlsx into production via Black Cat API (groups + contacts)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.import_bd import load_corporativo, load_productoras  # noqa: E402

API = "https://api.blackcathostal.com/api"
ADMIN_EMAIL = "admin@blackcathostal.com"
ADMIN_PASSWORD = "admin123"


def login(client: httpx.Client) -> dict[str, str]:
    response = client.post(
        "/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    response.raise_for_status()
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def ensure_group(client: httpx.Client, headers: dict[str, str], name: str, description: str) -> int:
    groups = client.get("/contact-groups/", headers=headers).json()
    for group in groups:
        if str(group.get("name", "")).strip().lower() == name.lower():
            return int(group["id"])
    created = client.post(
        "/contact-groups/",
        headers=headers,
        json={"name": name, "description": description, "is_active": True},
    )
    if created.status_code == 400 and "existe" in created.text.lower():
        groups = client.get("/contact-groups/", headers=headers).json()
        for group in groups:
            if str(group.get("name", "")).strip().lower() == name.lower():
                return int(group["id"])
    created.raise_for_status()
    return int(created.json()["id"])


def existing_emails(client: httpx.Client, headers: dict[str, str]) -> set[str]:
    contacts = client.get("/contacts/", headers=headers).json()
    return {str(c.get("email", "")).lower().strip() for c in contacts if c.get("email")}


def import_batch(
    client: httpx.Client,
    headers: dict[str, str],
    group_id: int,
    rows: list[tuple[str, str]],
    known_emails: set[str],
) -> tuple[int, int]:
    created = 0
    skipped = 0
    for full_name, email in rows:
        if email in known_emails:
            skipped += 1
            continue
        response = client.post(
            "/contacts/",
            headers=headers,
            json={
                "full_name": full_name,
                "email": email,
                "is_active": True,
                "group_id": group_id,
            },
        )
        if response.status_code == 400 and "already exists" in response.text.lower():
            skipped += 1
            known_emails.add(email)
            continue
        if response.status_code >= 400:
            print(f"  WARN {email}: {response.status_code} {response.text[:120]}")
            skipped += 1
            continue
        created += 1
        known_emails.add(email)
        if created % 50 == 0:
            print(f"  … {created} created")
            time.sleep(0.2)
    return created, skipped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=Path(r"C:\Users\jesus\Downloads\bd.xlsx"))
    parser.add_argument("--api", default=API)
    args = parser.parse_args()

    if not args.file.exists():
        raise SystemExit(f"File not found: {args.file}")

    corporativo_rows = load_corporativo(args.file)
    productoras_rows = load_productoras(args.file)
    print(f"File: {args.file}")
    print(f"Corporativo: {len(corporativo_rows)} emails")
    print(f"Productoras: {len(productoras_rows)} emails")

    with httpx.Client(base_url=args.api, timeout=60.0, follow_redirects=True) as client:
        headers = login(client)
        corp_id = ensure_group(
            client,
            headers,
            "Corporativo",
            "Contactos corporativos (bd.xlsx)",
        )
        prod_id = ensure_group(
            client,
            headers,
            "Productoras",
            "Productoras y agencias (bd.xlsx)",
        )
        print(f"Groups — Corporativo id={corp_id}, Productoras id={prod_id}")

        known = existing_emails(client, headers)
        print(f"Existing contacts in API: {len(known)}")

        c1, s1 = import_batch(client, headers, corp_id, corporativo_rows, known)
        c2, s2 = import_batch(client, headers, prod_id, productoras_rows, known)

        contacts = client.get("/contacts/", headers=headers).json()
        in_corp = sum(1 for c in contacts if c.get("group_id") == corp_id)
        in_prod = sum(1 for c in contacts if c.get("group_id") == prod_id)

        print(f"\nCorporativo — created: {c1}, skipped: {s1}, in group now: {in_corp}")
        print(f"Productoras — created: {c2}, skipped: {s2}, in group now: {in_prod}")
        print(f"Total contacts in API: {len(contacts)}")


if __name__ == "__main__":
    main()
