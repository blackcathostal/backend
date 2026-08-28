"""Apply Black Cat DB schema (create_all + patches + seed)."""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import inspect, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base, SessionLocal, engine
from app.main import _ensure_schema_patches
from app.models import (  # noqa: F401
    AiGenerationRuns,
    AiSources,
    AiUsage,
    Campaigns,
    ContactGroups,
    Contacts,
    MailAccounts,
    Medias,
    Posts,
    Roles,
    Rooms,
    Services,
    Sliders,
    Users,
)
from app.core.config import settings
from app.services.seed import seed_database


def main() -> None:
    url = settings.database_url
    # Redact password for display
    safe = url
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest and ":" in rest.split("@", 1)[0]:
            userpass, hostpart = rest.split("@", 1)
            user = userpass.split(":", 1)[0]
            safe = f"{scheme}://{user}:***@{hostpart}"

    print(f"Connecting to {safe}")
    with engine.connect() as conn:
        db_name = conn.execute(text("SELECT DATABASE()")).scalar()
        print(f"Database: {db_name}")

    print("Creating missing tables (create_all)...")
    Base.metadata.create_all(bind=engine)

    print("Applying schema patches...")
    _ensure_schema_patches()

    print("Seeding defaults...")
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()

    inspector = inspect(engine)
    tables = sorted(inspector.get_table_names())
    print(f"Tables ({len(tables)}): {', '.join(tables)}")

    # Key columns check
    with engine.connect() as conn:
        checks = [
            ("mail_accounts", "signature"),
            ("contacts", "group_id"),
            ("posts", "keywords"),
            ("posts", "image_source_url"),
            ("ai_usage", "api_requests"),
        ]
        for table, column in checks:
            exists = conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = :table_name
                      AND COLUMN_NAME = :column_name
                    """
                ),
                {"table_name": table, "column_name": column},
            ).scalar()
            print(f"  {table}.{column}: {'OK' if exists else 'MISSING'}")

        groups = conn.execute(text("SELECT id, name FROM contact_groups ORDER BY id")).fetchall()
        print(f"  contact_groups: {list(groups)}")

    print("Migrations applied successfully.")


if __name__ == "__main__":
    main()
