from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.api import api_router
from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
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
from app.services.mcp_sources import protected_mcp_app
from app.services.seed import seed_database

logger = logging.getLogger(__name__)


def _ensure_schema_patches() -> None:
    """create_all does not add columns to existing tables."""
    with engine.begin() as conn:

        def column_exists(table_name: str, column_name: str) -> bool:
            return bool(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM information_schema.COLUMNS
                        WHERE TABLE_SCHEMA = DATABASE()
                          AND TABLE_NAME = :table_name
                          AND COLUMN_NAME = :column_name
                        """
                    ),
                    {"table_name": table_name, "column_name": column_name},
                ).scalar()
            )

        if not column_exists("mail_accounts", "signature"):
            conn.execute(text("ALTER TABLE mail_accounts ADD COLUMN signature TEXT NULL"))
        if not column_exists("contacts", "group_id"):
            conn.execute(text("ALTER TABLE contacts ADD COLUMN group_id INT NULL"))
            conn.execute(
                text(
                    """
                    ALTER TABLE contacts
                    ADD CONSTRAINT fk_contacts_group_id
                    FOREIGN KEY (group_id) REFERENCES contact_groups(id)
                    ON DELETE SET NULL
                    """
                )
            )
        if not column_exists("posts", "keywords"):
            conn.execute(
                text("ALTER TABLE posts ADD COLUMN keywords VARCHAR(500) NOT NULL DEFAULT ''")
            )
        if not column_exists("posts", "image_source_url"):
            conn.execute(
                text(
                    "ALTER TABLE posts ADD COLUMN image_source_url VARCHAR(500) "
                    "NOT NULL DEFAULT ''"
                )
            )


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        Base.metadata.create_all(bind=engine)
        _ensure_schema_patches()
        db = SessionLocal()
        try:
            seed_database(db)
        finally:
            db.close()
    except OperationalError:
        logger.warning("Database unavailable at startup; API will run without MySQL.")
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.api_prefix)
app.mount("/uploads", StaticFiles(directory=str(settings.uploads_dir)), name="uploads")
app.mount("/mcp", protected_mcp_app())


@app.get("/")
def root() -> dict[str, str]:
    return {"message": f"Welcome to {settings.app_name}"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
    )
