import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.api import api_router
from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.models import (  # noqa: F401
    AccountingAccounts,
    AiGenerationRuns,
    AiSources,
    AiUsage,
    Campaigns,
    Complaints,
    ContactGroups,
    ContactInquiries,
    Contacts,
    DteStatuses,
    DteTypes,
    TaxDocuments,
    MailAccounts,
    Medias,
    Posts,
    Roles,
    Rooms,
    Services,
    Sliders,
    TermsAndConditions,
    Users,
    VisitorEvents,
    VisitorSessions,
)
from app.models.hostel_info import CommonAnswers, HostelInfo  # noqa: F401
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
        if not column_exists("rooms", "features"):
            conn.execute(text("ALTER TABLE rooms ADD COLUMN features TEXT NULL"))
        if not column_exists("rooms", "photos"):
            try:
                conn.execute(text("ALTER TABLE rooms ADD COLUMN photos TEXT NULL"))
            except Exception:  # noqa: BLE001
                logger.exception("Could not add rooms.photos column")
            try:
                conn.execute(text("UPDATE rooms SET photos = '[]' WHERE photos IS NULL"))
            except Exception:  # noqa: BLE001
                logger.exception("Could not backfill rooms.photos")
        if column_exists("rooms", "status"):
            conn.execute(text("ALTER TABLE rooms DROP COLUMN status"))
        if column_exists("rooms", "caracteristicas") and column_exists("rooms", "features"):
            conn.execute(
                text(
                    """
                    UPDATE rooms
                    SET features = caracteristicas
                    WHERE (features IS NULL OR features = '')
                      AND caracteristicas IS NOT NULL
                      AND caracteristicas <> ''
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
        if not column_exists("posts", "reddit_url"):
            conn.execute(
                text("ALTER TABLE posts ADD COLUMN reddit_url VARCHAR(500) NOT NULL DEFAULT ''")
            )
        if not column_exists("posts", "reddit_subreddit"):
            conn.execute(
                text(
                    "ALTER TABLE posts ADD COLUMN reddit_subreddit VARCHAR(80) NOT NULL DEFAULT ''"
                )
            )
        if not column_exists("posts", "reddit_published_at"):
            conn.execute(text("ALTER TABLE posts ADD COLUMN reddit_published_at DATETIME NULL"))
        if not column_exists("ai_usage", "api_requests"):
            conn.execute(
                text(
                    "ALTER TABLE ai_usage ADD COLUMN api_requests INT NOT NULL DEFAULT 1"
                )
            )
        if not column_exists("ai_usage", "platform_cost_usd"):
            conn.execute(
                text("ALTER TABLE ai_usage ADD COLUMN platform_cost_usd FLOAT NULL")
            )
        if not column_exists("ai_usage", "platform_balance_usd"):
            conn.execute(
                text("ALTER TABLE ai_usage ADD COLUMN platform_balance_usd FLOAT NULL")
            )
        if not column_exists("ai_generation_runs", "generated_title"):
            conn.execute(
                text("ALTER TABLE ai_generation_runs ADD COLUMN generated_title VARCHAR(220) NULL")
            )
        if not column_exists("ai_generation_runs", "generated_excerpt"):
            conn.execute(
                text("ALTER TABLE ai_generation_runs ADD COLUMN generated_excerpt TEXT NULL")
            )
        if not column_exists("ai_generation_runs", "generated_keywords"):
            conn.execute(
                text("ALTER TABLE ai_generation_runs ADD COLUMN generated_keywords VARCHAR(500) NULL")
            )
        if not column_exists("ai_generation_runs", "generated_body"):
            conn.execute(
                text("ALTER TABLE ai_generation_runs ADD COLUMN generated_body TEXT NULL")
            )
        if not column_exists("visitor_events", "duration_seconds"):
            conn.execute(
                text(
                    "ALTER TABLE visitor_events ADD COLUMN duration_seconds INT NOT NULL DEFAULT 0"
                )
            )
        if not column_exists("visitor_sessions", "ended_at"):
            conn.execute(text("ALTER TABLE visitor_sessions ADD COLUMN ended_at DATETIME NULL"))
        if not column_exists("complaints", "data_consent"):
            conn.execute(
                text(
                    "ALTER TABLE complaints ADD COLUMN data_consent TINYINT(1) "
                    "NOT NULL DEFAULT 0"
                )
            )
        if not column_exists("complaints", "data_consent_accepted_at"):
            conn.execute(
                text("ALTER TABLE complaints ADD COLUMN data_consent_accepted_at DATETIME NULL")
            )
        if not column_exists("accounting_accounts", "category"):
            conn.execute(
                text(
                    "ALTER TABLE accounting_accounts "
                    "ADD COLUMN category VARCHAR(80) NOT NULL DEFAULT ''"
                )
            )
        if not column_exists("accounting_accounts", "sort_order"):
            conn.execute(
                text(
                    "ALTER TABLE accounting_accounts "
                    "ADD COLUMN sort_order INT NOT NULL DEFAULT 0"
                )
            )
        def table_exists(table_name: str) -> bool:
            return bool(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM information_schema.TABLES
                        WHERE TABLE_SCHEMA = DATABASE()
                          AND TABLE_NAME = :table_name
                        """
                    ),
                    {"table_name": table_name},
                ).scalar()
            )

        # Rename legacy accounting_track_documents → tax_documents
        if table_exists("accounting_track_documents") and not table_exists("tax_documents"):
            conn.execute(text("RENAME TABLE accounting_track_documents TO tax_documents"))

        if not column_exists("tax_documents", "dte_type_id"):
            conn.execute(text("ALTER TABLE tax_documents ADD COLUMN dte_type_id INT NULL"))
        if not column_exists("tax_documents", "status_id"):
            conn.execute(text("ALTER TABLE tax_documents ADD COLUMN status_id INT NULL"))
        if not column_exists("tax_documents", "period"):
            conn.execute(
                text(
                    "ALTER TABLE tax_documents "
                    "ADD COLUMN period VARCHAR(40) NOT NULL DEFAULT ''"
                )
            )

@asynccontextmanager
async def lifespan(_: FastAPI):
    import asyncio

    comment_poll_task: asyncio.Task | None = None
    try:
        Base.metadata.create_all(bind=engine)
        try:
            _ensure_schema_patches()
        except Exception:  # noqa: BLE001
            # Never take the API down for a column patch (e.g. rooms.photos).
            logger.exception("Schema patch failed at startup; continuing")
        db = SessionLocal()
        try:
            seed_database(db)
        finally:
            db.close()
    except OperationalError:
        logger.warning("Database unavailable at startup; API will run without MySQL.")
    except Exception:  # noqa: BLE001
        logger.exception("Startup DB init failed; API will continue")

    try:
        from app.services.instagram import poll_instagram_comments_loop

        comment_poll_task = asyncio.create_task(
            poll_instagram_comments_loop(),
            name="instagram-comment-poll",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not start Instagram comment poller")

    try:
        yield
    finally:
        if comment_poll_task is not None:
            comment_poll_task.cancel()
            try:
                await comment_poll_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("Instagram comment poller shutdown error")


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
