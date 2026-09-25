"""Facebook Page admin API: posts and comments."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.security import get_current_user
from app.models.users import Users
from app.services import facebook as fb

router = APIRouter(prefix="/facebook", tags=["facebook"])


class PostBody(BaseModel):
    message: str = Field(default="", max_length=5000)
    link: str = Field(default="", max_length=2000)


class CommentBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


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
    _: Users = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        items = await fb.list_posts(limit=limit)
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
