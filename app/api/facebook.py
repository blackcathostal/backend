"""Facebook Page admin API: posts, comments, Messenger and DeepSeek inbox."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.security import get_current_user
from app.models.users import Users
from app.services import facebook as fb
from app.services.deepseek_instagram import DeepSeekConfigError, DeepSeekError

router = APIRouter(prefix="/facebook", tags=["facebook"])


class PostBody(BaseModel):
    message: str = Field(default="", max_length=5000)
    link: str = Field(default="", max_length=2000)


class CommentBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class InboxTextBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, fb.FacebookConfigError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, fb.FacebookError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="Error de Facebook")


@router.get("/status")
def facebook_status(_: Users = Depends(get_current_user)) -> dict[str, Any]:
    return fb.status()


@router.get("/posts")
async def facebook_posts(
    limit: int = Query(default=25, ge=1, le=50),
    from_admin: bool = Query(default=False),
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        items = await fb.list_admin_posts(limit=limit) if from_admin else await fb.list_posts(limit=limit)
        return {"items": items, "count": len(items)}
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.post("/posts")
async def facebook_create_post(
    body: PostBody,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await fb.create_post(body.message, body.link)
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.put("/posts/{post_id}")
async def facebook_update_post(
    post_id: str,
    body: PostBody,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await fb.update_post(post_id, body.message)
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.delete("/posts/{post_id}")
async def facebook_delete_post(
    post_id: str,
    _: Users = Depends(get_current_user),
) -> dict[str, bool]:
    try:
        return await fb.delete_post(post_id)
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.get("/posts/{post_id}")
async def facebook_post(
    post_id: str,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await fb.list_comments(post_id)
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.post("/posts/{post_id}/comments")
async def facebook_comment_on_post(
    post_id: str,
    body: CommentBody,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await fb.comment_on_post(post_id, body.message)
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.put("/comments/{comment_id}")
async def facebook_update_comment(
    comment_id: str,
    body: CommentBody,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await fb.update_comment(comment_id, body.message)
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.delete("/comments/{comment_id}")
async def facebook_delete_comment(
    comment_id: str,
    _: Users = Depends(get_current_user),
) -> dict[str, bool]:
    try:
        return await fb.delete_comment(comment_id)
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.get("/messenger")
def facebook_messenger(_: Users = Depends(get_current_user)) -> dict[str, Any]:
    items = fb.list_messenger_threads()
    return {"items": items, "count": len(items)}


@router.post("/messenger/sync")
async def facebook_sync_messenger(_: Users = Depends(get_current_user)) -> dict[str, Any]:
    try:
        return await fb.sync_messenger()
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.get("/messenger/{thread_id}")
def facebook_messenger_thread(
    thread_id: str,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return fb.get_messenger_thread(thread_id)
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.post("/messenger/{thread_id}/reply")
async def facebook_messenger_reply(
    thread_id: str,
    body: CommentBody,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await fb.reply_messenger(thread_id, body.message)
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.delete("/messenger/{thread_id}/messages/{message_id}")
def facebook_messenger_delete(
    thread_id: str,
    message_id: str,
    _: Users = Depends(get_current_user),
) -> dict[str, bool]:
    try:
        fb.refuse_messenger_delete()
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc
    return {"ok": False}


@router.post("/comments/{comment_id}/reply")
async def facebook_reply_comment(
    comment_id: str,
    body: CommentBody,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await fb.reply_to_comment(comment_id, body.message)
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.get("/inbox")
def facebook_inbox(
    type: str | None = Query(default=None, alias="type"),
    status: str | None = None,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    items = fb.list_inbox_page(item_type=type, status=status)
    return {"items": items, "count": len(items)}


@router.get("/inbox/{item_id}")
def facebook_inbox_item(item_id: str, _: Users = Depends(get_current_user)) -> dict[str, Any]:
    try:
        return fb.get_inbox_detail(item_id)
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.post("/inbox/{item_id}/reply")
async def facebook_inbox_reply(
    item_id: str,
    body: CommentBody,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await fb.reply_inbox(item_id, body.message, source="human")
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.post("/inbox/{item_id}/suggest")
async def facebook_inbox_suggest(
    item_id: str,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await fb.suggest_inbox_reply(item_id)
    except DeepSeekConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DeepSeekError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.post("/inbox/{item_id}/auto-reply")
async def facebook_inbox_auto_reply(
    item_id: str,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await fb.auto_inbox_reply(item_id)
    except DeepSeekConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DeepSeekError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.put("/inbox/{item_id}/replies/{reply_index}")
async def facebook_inbox_update_reply(
    item_id: str,
    reply_index: int,
    body: InboxTextBody,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await fb.update_inbox_reply(item_id, reply_index, body.text)
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.delete("/inbox/{item_id}/replies/{reply_index}")
async def facebook_inbox_delete_reply(
    item_id: str,
    reply_index: int,
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await fb.delete_inbox_reply(item_id, reply_index)
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc


@router.delete("/inbox/{item_id}")
async def facebook_inbox_delete(
    item_id: str,
    _: Users = Depends(get_current_user),
) -> dict[str, bool]:
    try:
        await fb.delete_inbox_item(item_id)
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc
    return {"ok": True}


@router.post("/sync-inbox")
async def facebook_sync_inbox(_: Users = Depends(get_current_user)) -> dict[str, Any]:
    try:
        return await fb.sync_inbox()
    except (fb.FacebookConfigError, fb.FacebookError) as exc:
        raise _http(exc) from exc
