from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import jwt
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.ai_sources import AiSources
from app.models.users import Users
from app.services.ai_source_fetcher import fetch_source_content

mcp = FastMCP(
    "Black Cat Tourism Sources",
    instructions="Consulta únicamente las fuentes web HTTPS activadas por el administrador.",
    streamable_http_path="/",
)


@mcp.tool(
    name="list_active_sources",
    description="Lista las fuentes HTTPS activas ordenadas por prioridad.",
    structured_output=True,
)
def list_active_sources(limit: int = 6) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit), settings.deepseek_max_sources))
    with SessionLocal() as db:
        rows = (
            db.query(AiSources)
            .filter(AiSources.is_active.is_(True))
            .order_by(AiSources.priority.desc(), AiSources.id.asc())
            .limit(safe_limit)
            .all()
        )
        return {
            "sources": [
                {
                    "id": row.id,
                    "name": row.name,
                    "url": row.url,
                    "category": row.category,
                    "language": row.language,
                }
                for row in rows
            ]
        }


@mcp.tool(
    name="read_source",
    description="Lee el texto útil de una fuente activa configurada.",
    structured_output=True,
)
async def read_source(source_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        source = (
            db.query(AiSources)
            .filter(AiSources.id == int(source_id), AiSources.is_active.is_(True))
            .first()
        )
        if not source:
            return {"ok": False, "source_id": source_id, "error": "Fuente no encontrada o inactiva."}
        url = source.url
        source_name = source.name

    try:
        material = await fetch_source_content(
            url,
            timeout_seconds=settings.deepseek_source_timeout_seconds,
            max_bytes=settings.deepseek_source_max_bytes,
        )
    except Exception as exc:
        with SessionLocal() as db:
            failed = db.query(AiSources).filter(AiSources.id == int(source_id)).first()
            if failed:
                failed.last_checked_at = datetime.now(timezone.utc)
                failed.last_status = "error"
                failed.last_error = str(exc)[:1000]
                db.commit()
        return {"ok": False, "source_id": source_id, "name": source_name, "error": str(exc)}

    with SessionLocal() as db:
        checked = db.query(AiSources).filter(AiSources.id == int(source_id)).first()
        if checked:
            checked.last_checked_at = datetime.now(timezone.utc)
            checked.last_status = "ok"
            checked.last_error = None
            db.commit()

    return {
        "ok": True,
        "source_id": source_id,
        "name": source_name,
        "url": material["url"],
        "title": material["title"],
        "text": material["text"][: settings.deepseek_source_char_limit],
        "image_url": material.get("image_url", ""),
        "image_urls": material.get("image_urls", []),
    }


async def collect_source_material() -> tuple[list[dict[str, Any]], list[int]]:
    def payload_from_result(result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result
        if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
            return result[1]
        structured = getattr(result, "structured_content", None)
        return structured if isinstance(structured, dict) else {}

    listed = await mcp.call_tool(
        "list_active_sources",
        {"limit": settings.deepseek_max_sources},
    )
    source_payload = payload_from_result(listed)
    materials: list[dict[str, Any]] = []
    source_ids: list[int] = []
    for source in source_payload.get("sources", []):
        result = await mcp.call_tool("read_source", {"source_id": int(source["id"])})
        result_payload = payload_from_result(result)
        if result_payload.get("ok"):
            materials.append(result_payload)
            source_ids.append(int(source["id"]))
    return materials, source_ids


def protected_mcp_app():
    mcp_app = mcp.streamable_http_app()

    async def application(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            authorization = headers.get(b"authorization", b"").decode("latin-1")
            token = authorization.removeprefix("Bearer ").strip()
            authenticated = False
            if token:
                try:
                    payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
                    email = payload.get("sub")
                    with SessionLocal() as db:
                        user = (
                            db.query(Users)
                            .filter(Users.email == email, Users.is_active.is_(True))
                            .first()
                        )
                        authenticated = bool(user)
                except (jwt.PyJWTError, TypeError, ValueError):
                    authenticated = False
            if not authenticated:
                await JSONResponse(
                    {"detail": "Unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return
        await mcp_app(scope, receive, send)

    return application
