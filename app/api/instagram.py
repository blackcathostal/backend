"""Instagram admin + webhook API."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.users import Users
from app.services import instagram as ig
from app.services import instagram_knowledge as knowledge
from app.services import room_rates as rates
from app.services.deepseek_instagram import DeepSeekConfigError, DeepSeekError

router = APIRouter(prefix="/instagram", tags=["instagram"])


class ReplyBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class RoomRatesBody(BaseModel):
    currency: str = "CLP"
    note: str = ""
    includes: list[str] = Field(default_factory=list)
    contact: dict[str, str] = Field(default_factory=dict)
    rooms: list[dict[str, Any]]


@router.get("/status")
def instagram_status(_: Users = Depends(get_current_user)) -> dict[str, Any]:
    return ig.connection_status()


@router.get("/auth-url")
def instagram_auth_url(_: Users = Depends(get_current_user)) -> dict[str, str]:
    try:
        return {"url": ig.build_auth_url()}
    except ig.InstagramConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/callback")
async def instagram_callback(
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    admin = (
        (settings.instagram_admin_redirect or "").rstrip("/")
        or "http://localhost:5174/settings/instagram-knowledge"
    )
    if error:
        qs = urlencode({"connected": "0", "error": error_description or error})
        return RedirectResponse(f"{admin}?{qs}")
    if not code:
        qs = urlencode({"connected": "0", "error": "missing_code"})
        return RedirectResponse(f"{admin}?{qs}")
    try:
        await ig.exchange_code_and_connect(code)
        qs = urlencode({"connected": "1"})
        return RedirectResponse(f"{admin}?{qs}")
    except (ig.InstagramConfigError, ig.InstagramError) as exc:
        qs = urlencode({"connected": "0", "error": str(exc)[:300]})
        return RedirectResponse(f"{admin}?{qs}")


@router.delete("/disconnect")
def instagram_disconnect(_: Users = Depends(get_current_user)) -> dict[str, bool]:
    ig.clear_connection()
    return {"ok": True}


@router.get("/inbox")
def get_inbox(
    type: str | None = Query(default=None, alias="type"),
    status: str | None = None,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    items = ig.list_inbox(item_type=type, status=status)
    return {"items": items, "count": len(items)}


@router.get("/inbox/{item_id}")
def get_inbox_item(item_id: str, _: Users = Depends(get_current_user)) -> dict[str, Any]:
    item = ig.get_inbox_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="No encontrado")
    return item


@router.post("/inbox/{item_id}/reply")
async def reply_inbox_item(
    item_id: str,
    body: ReplyBody,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await ig.send_inbox_reply(item_id, body.message, source="human")
    except ig.InstagramConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ig.InstagramError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/inbox/{item_id}/suggest")
async def suggest_inbox_item(
    item_id: str,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await ig.suggest_reply(item_id)
    except DeepSeekConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DeepSeekError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ig.InstagramError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/inbox/{item_id}/auto-reply")
async def auto_reply_inbox_item(
    item_id: str,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await ig.run_auto_reply(item_id)
    except DeepSeekConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DeepSeekError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ig.InstagramConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ig.InstagramError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sync-comments")
async def sync_comments(_: Users = Depends(get_current_user)) -> dict[str, Any]:
    try:
        synced = await ig.sync_recent_comments()
        return {"synced": len(synced), "items": synced}
    except ig.InstagramConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ig.InstagramError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/room-rates")
def get_room_rates(_: Users = Depends(get_current_user)) -> dict[str, Any]:
    return rates.rates_public_payload()


@router.put("/room-rates")
def put_room_rates(
    body: RoomRatesBody,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return rates.save_room_rates(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class HostelInfoBody(BaseModel):
    check_in_time: str = "15:00"
    check_out_time: str = "12:00"
    breakfast_hours: str = "08:00-10:00"
    has_parking: bool = False
    parking_notes: str = ""
    whatsapp_url: str = "https://wa.me/56949105984"
    address: str = ""
    email: str = "reservas@blackcathostal.com"
    website: str = "https://blackcathostal.com"
    extra_notes: str = ""


class CommonAnswerBody(BaseModel):
    topic: str = "general"
    keywords: str = ""
    question: str = Field(min_length=1, max_length=255)
    answer_guide: str = Field(min_length=1)
    sort_order: int = 0
    is_active: bool = True


@router.get("/hostel-info")
def get_hostel_info(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    return knowledge.hostel_info_dict(db)


@router.put("/hostel-info")
def put_hostel_info(
    body: HostelInfoBody,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    return knowledge.update_hostel_info(db, body.model_dump())


@router.get("/common-answers")
def get_common_answers(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    items = knowledge.list_common_answers_admin(db)
    return {"items": items, "count": len(items)}


@router.post("/common-answers")
def create_common_answer(
    body: CommonAnswerBody,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return knowledge.upsert_common_answer(db, body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/common-answers/{answer_id}")
def update_common_answer(
    answer_id: int,
    body: CommonAnswerBody,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return knowledge.upsert_common_answer(db, body.model_dump(), answer_id=answer_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/common-answers/{answer_id}")
def remove_common_answer(
    answer_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> dict[str, bool]:
    try:
        knowledge.delete_common_answer(db, answer_id)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/webhook")
def verify_webhook(request: Request) -> Any:
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    expected = (settings.meta_webhook_verify_token or "").strip()
    if mode == "subscribe" and token and expected and token == expected and challenge is not None:
        return PlainTextResponse(content=str(challenge))
    raise HTTPException(status_code=403, detail="Webhook verification failed")


@router.post("/webhook")
async def receive_webhook(payload: dict[str, Any]) -> dict[str, bool]:
    try:
        await ig.process_webhook_payload(payload)
    except Exception:
        pass
    return {"ok": True}
