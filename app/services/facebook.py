"""Facebook Page Graph API: posts and comments, using the Meta page token."""

from __future__ import annotations

from typing import Any

from app.services.instagram import (
    InstagramConfigError,
    InstagramError,
    _graph_get,
    _graph_post,
    _require_connection,
    connection_status,
)

FacebookConfigError = InstagramConfigError
FacebookError = InstagramError

POST_FIELDS = (
    "id,message,story,created_time,permalink_url,full_picture,status_type,"
    "comments.summary(true).limit(0)"
)
COMMENT_FIELDS = (
    "id,from{id,name},message,created_time,comment_count,parent{id},permalink_url"
)


def status() -> dict[str, Any]:
    data = connection_status()
    return {
        "meta_configured": data.get("meta_configured"),
        "connected": data.get("connected"),
        "page_id": data.get("page_id"),
        "page_name": data.get("page_name"),
        "connected_at": data.get("connected_at"),
        "hint": data.get("hint"),
    }


def _serialize_post(post: dict[str, Any]) -> dict[str, Any]:
    summary = ((post.get("comments") or {}).get("summary") or {})
    return {
        "id": post.get("id") or "",
        "message": (post.get("message") or post.get("story") or "").strip(),
        "created_time": post.get("created_time") or "",
        "permalink_url": post.get("permalink_url") or "",
        "picture": post.get("full_picture") or "",
        "status_type": post.get("status_type") or "",
        "comment_count": int(summary.get("total_count") or 0),
    }


def _serialize_comment(comment: dict[str, Any]) -> dict[str, Any]:
    author = comment.get("from") or {}
    parent = comment.get("parent") or {}
    return {
        "id": comment.get("id") or "",
        "message": comment.get("message") or "",
        "created_time": comment.get("created_time") or "",
        "author_id": author.get("id") or "",
        "author_name": author.get("name") or "Usuario",
        "comment_count": int(comment.get("comment_count") or 0),
        "parent_id": parent.get("id") or "",
        "permalink_url": comment.get("permalink_url") or "",
    }


async def list_posts(limit: int = 25) -> list[dict[str, Any]]:
    conn = _require_connection()
    page_id = conn["page_id"]
    token = conn["page_access_token"]
    params = {"fields": POST_FIELDS, "limit": max(1, min(limit, 50))}
    try:
        data = await _graph_get(f"/{page_id}/published_posts", token, params)
    except InstagramError:
        data = await _graph_get(f"/{page_id}/posts", token, params)
    return [_serialize_post(item) for item in data.get("data") or [] if item.get("id")]


async def get_post(post_id: str) -> dict[str, Any]:
    conn = _require_connection()
    data = await _graph_get(
        f"/{post_id}",
        conn["page_access_token"],
        {"fields": POST_FIELDS},
    )
    if not data.get("id"):
        raise InstagramError("Publicación no encontrada")
    return _serialize_post(data)


async def list_comments(post_id: str, limit: int = 100) -> dict[str, Any]:
    conn = _require_connection()
    token = conn["page_access_token"]
    post = await get_post(post_id)
    data = await _graph_get(
        f"/{post_id}/comments",
        token,
        {
            "fields": COMMENT_FIELDS,
            "filter": "stream",
            "order": "chronological",
            "limit": max(1, min(limit, 100)),
        },
    )
    comments = [_serialize_comment(item) for item in data.get("data") or [] if item.get("id")]
    return {"post": post, "comments": comments, "count": len(comments)}


async def create_post(message: str, link: str = "") -> dict[str, Any]:
    conn = _require_connection()
    text = (message or "").strip()
    url = (link or "").strip()
    if not text and not url:
        raise InstagramError("Escribe un texto o un enlace para publicar")
    payload: dict[str, Any] = {}
    if text:
        payload["message"] = text
    if url:
        payload["link"] = url
    result = await _graph_post(f"/{conn['page_id']}/feed", conn["page_access_token"], payload)
    post_id = result.get("id")
    if post_id:
        try:
            return await get_post(post_id)
        except InstagramError:
            pass
    return {
        "id": post_id or "",
        "message": text,
        "created_time": "",
        "permalink_url": "",
        "picture": "",
        "status_type": "",
        "comment_count": 0,
    }


async def comment_on_post(post_id: str, message: str) -> dict[str, Any]:
    conn = _require_connection()
    text = (message or "").strip()
    if not text:
        raise InstagramError("El comentario no puede estar vacío")
    result = await _graph_post(
        f"/{post_id}/comments",
        conn["page_access_token"],
        {"message": text},
    )
    return {"id": result.get("id") or "", "message": text}


async def reply_to_comment(comment_id: str, message: str) -> dict[str, Any]:
    conn = _require_connection()
    text = (message or "").strip()
    if not text:
        raise InstagramError("La respuesta no puede estar vacía")
    result = await _graph_post(
        f"/{comment_id}/comments",
        conn["page_access_token"],
        {"message": text},
    )
    return {"id": result.get("id") or "", "message": text}
