"""Facebook Page Graph API: posts, comments and private Messenger threads."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.services.instagram import (
    InstagramConfigError,
    InstagramError,
    _graph_delete,
    _graph_get,
    _graph_pages,
    _graph_post,
    _require_connection,
    connection_status,
    load_connection,
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


def _serialize_comment(
    comment: dict[str, Any],
    *,
    page_id: str = "",
    page_name: str = "",
) -> dict[str, Any]:
    author = comment.get("from") or {}
    parent = comment.get("parent") or {}
    author_id = str(author.get("id") or "")
    author_name = author.get("name") or "Usuario"
    own = bool(page_id) and author_id == str(page_id)
    if not own and page_name and author_name.strip().lower() == page_name.strip().lower():
        own = True
    return {
        "id": comment.get("id") or "",
        "message": comment.get("message") or "",
        "created_time": comment.get("created_time") or "",
        "author_id": author_id,
        "author_name": author_name,
        "own": own,
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
    page_id = str(conn.get("page_id") or "")
    page_name = str(conn.get("page_name") or "")
    params = {
        "fields": COMMENT_FIELDS,
        "filter": "stream",
        "order": "chronological",
        "limit": max(1, min(limit, 100)),
    }
    try:
        rows = await _graph_pages(f"/{post_id}/comments", token, params, max_pages=10)
    except InstagramError:
        data = await _graph_get(f"/{post_id}/comments", token, params)
        rows = data.get("data") or []
    seen = {str(item.get("id") or "") for item in rows if item.get("id")}
    extra: list[dict[str, Any]] = []
    for item in rows:
        if int(item.get("comment_count") or 0) <= 0 or not item.get("id"):
            continue
        try:
            replies = await _graph_pages(
                f"/{item['id']}/comments",
                token,
                {"fields": COMMENT_FIELDS, "limit": 50},
                max_pages=4,
            )
        except InstagramError:
            continue
        for reply in replies:
            reply_id = str(reply.get("id") or "")
            if reply_id and reply_id not in seen:
                seen.add(reply_id)
                extra.append(reply)
    comments = [
        _serialize_comment(item, page_id=page_id, page_name=page_name)
        for item in [*rows, *extra]
        if item.get("id")
    ]
    comments.sort(key=lambda item: item.get("created_time") or "")
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


async def update_comment(comment_id: str, message: str) -> dict[str, Any]:
    conn = _require_connection()
    text = (message or "").strip()
    if not text:
        raise InstagramError("El comentario no puede quedar vacío")
    token = conn["page_access_token"]
    page_id = str(conn.get("page_id") or "")
    current = await _graph_get(f"/{comment_id}", token, {"fields": "id,from{id,name},message"})
    author = current.get("from") or {}
    author_id = str(author.get("id") or "")
    author_name = str(author.get("name") or "")
    page_name = str(conn.get("page_name") or "")
    own = bool(page_id) and author_id == page_id
    if not own and page_name and author_name.strip().lower() == page_name.strip().lower():
        own = True
    if not own:
        raise InstagramError("Solo puedes editar los comentarios de la página")
    await _graph_post(f"/{comment_id}", token, {"message": text})
    return {"id": comment_id, "message": text, "own": True}


async def delete_comment(comment_id: str) -> dict[str, bool]:
    conn = _require_connection()
    result = await _graph_delete(f"/{comment_id}", conn["page_access_token"])
    if result.get("success") is False:
        err = result.get("error") or {}
        raise InstagramError(err.get("message") or "Facebook no borró el comentario")
    return {"ok": True}


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


MESSENGER_FILE = settings.uploads_dir / "cache" / "facebook_messenger.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_messenger() -> dict[str, Any]:
    MESSENGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not MESSENGER_FILE.exists():
        return {"threads": []}
    try:
        data = json.loads(MESSENGER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"threads": []}
    if not isinstance(data, dict) or not isinstance(data.get("threads"), list):
        return {"threads": []}
    return data


def _save_messenger(data: dict[str, Any]) -> None:
    MESSENGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    MESSENGER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _thread_preview(thread: dict[str, Any]) -> dict[str, Any]:
    messages = list(thread.get("messages") or [])
    last = messages[-1] if messages else {}
    return {
        "id": thread.get("id") or "",
        "peer_id": thread.get("peer_id") or "",
        "peer_name": thread.get("peer_name") or "Persona",
        "updated_time": thread.get("updated_time") or last.get("created_time") or "",
        "last_message": last.get("text") or "",
        "message_count": len(messages),
    }


def list_messenger_threads() -> list[dict[str, Any]]:
    threads = list(_load_messenger().get("threads") or [])
    threads.sort(key=lambda row: row.get("updated_time") or "", reverse=True)
    return [_thread_preview(thread) for thread in threads]


def get_messenger_thread(thread_id: str) -> dict[str, Any]:
    for thread in _load_messenger().get("threads") or []:
        if str(thread.get("id") or "") == str(thread_id):
            return thread
    raise InstagramError("Conversación de Messenger no encontrada")


def _upsert_thread(thread: dict[str, Any]) -> None:
    data = _load_messenger()
    threads = list(data.get("threads") or [])
    replaced = False
    for index, existing in enumerate(threads):
        same_id = existing.get("id") and existing.get("id") == thread.get("id")
        same_peer = existing.get("peer_id") and existing.get("peer_id") == thread.get("peer_id")
        if same_id or same_peer:
            threads[index] = thread
            replaced = True
            break
    if not replaced:
        threads.append(thread)
    threads.sort(key=lambda row: row.get("updated_time") or "", reverse=True)
    data["threads"] = threads[:80]
    _save_messenger(data)


async def _hydrate_messenger_message(msg: dict[str, Any], token: str) -> dict[str, Any]:
    mid = str(msg.get("id") or "").strip()
    sender = msg.get("from") if isinstance(msg.get("from"), dict) else {}
    if not mid or (sender.get("id") and "message" in msg):
        return msg
    try:
        detail = await _graph_get(f"/{mid}", token, {"fields": "id,message,from,created_time"})
    except InstagramError:
        return msg
    if not isinstance(detail, dict):
        return msg
    merged = dict(msg)
    for key, value in detail.items():
        if value is not None:
            merged[key] = value
    return merged


async def sync_messenger(limit_conversations: int = 25) -> dict[str, Any]:
    """Pull private Facebook Messenger threads. Does not include Instagram DMs."""
    conn = _require_connection()
    token = conn["page_access_token"]
    page_id = str(conn.get("page_id") or "")
    params = {
        "platform": "messenger",
        "fields": "id,updated_time,participants,snippet",
        "limit": max(1, min(limit_conversations, 25)),
    }
    try:
        conversations = await _graph_pages(f"/{page_id}/conversations", token, params, max_pages=10)
    except InstagramError as exc:
        raise InstagramError(
            "No se pudieron leer los mensajes privados de Messenger. "
            "Vuelve a conectar Meta para autorizar pages_messaging. "
            f"Detalle: {exc}"
        ) from exc
    own_ids = {page_id} if page_id else set()
    synced = 0
    for conv in conversations:
        thread_id = str(conv.get("id") or "")
        if not thread_id:
            continue
        people = [
            person
            for person in ((conv.get("participants") or {}).get("data") or [])
            if person.get("id") and str(person.get("id")) not in own_ids
        ]
        peer = people[0] if people else {}
        peer_id = str(peer.get("id") or "")
        peer_name = str(peer.get("name") or peer.get("username") or "Persona")
        try:
            raw_messages = await _graph_pages(
                f"/{thread_id}/messages",
                token,
                {"fields": "id,message,from,created_time", "limit": 50},
                max_pages=8,
            )
        except InstagramError:
            try:
                raw_messages = await _graph_pages(
                    f"/{thread_id}/messages",
                    token,
                    {"fields": "id,created_time", "limit": 50},
                    max_pages=8,
                )
            except InstagramError:
                raw_messages = []
        bubbles: list[dict[str, Any]] = []
        for msg in raw_messages:
            msg = await _hydrate_messenger_message(msg, token)
            sender = msg.get("from") if isinstance(msg.get("from"), dict) else {}
            sender_id = str(sender.get("id") or "")
            text = str(msg.get("message") or "").strip() or "[archivo]"
            bubbles.append(
                {
                    "id": str(msg.get("id") or uuid.uuid4()),
                    "text": text,
                    "created_time": msg.get("created_time") or "",
                    "own": sender_id in own_ids,
                    "author_name": "Página" if sender_id in own_ids else (sender.get("name") or peer_name),
                }
            )
        bubbles.sort(key=lambda row: row.get("created_time") or "")
        _upsert_thread(
            {
                "id": thread_id,
                "peer_id": peer_id,
                "peer_name": peer_name,
                "updated_time": conv.get("updated_time") or _now_iso(),
                "messages": bubbles[-300:],
            }
        )
        synced += 1
    return {"synced": synced, "threads": list_messenger_threads()}


def ingest_messenger_webhook(payload: dict[str, Any]) -> int:
    """Store private Messenger texts that arrive on the Page webhook."""
    if str(payload.get("object") or "") != "page":
        return 0
    conn = load_connection() or {}
    page_id = str(conn.get("page_id") or "")
    stored = 0
    for entry in payload.get("entry") or []:
        for event in (entry.get("messaging") or []) + (entry.get("standby") or []):
            message = event.get("message")
            if not isinstance(message, dict) or message.get("is_deleted"):
                continue
            sender = str((event.get("sender") or {}).get("id") or "")
            recipient = str((event.get("recipient") or {}).get("id") or "")
            text = str(message.get("text") or "").strip()
            if not text and message.get("attachments"):
                text = "[archivo]"
            if not sender or not text:
                continue
            own = (bool(page_id) and sender == page_id) or bool(message.get("is_echo"))
            peer_id = recipient if own else sender
            if not peer_id or peer_id == page_id:
                continue
            data = _load_messenger()
            threads = list(data.get("threads") or [])
            thread = next((row for row in threads if row.get("peer_id") == peer_id), None)
            if thread is None:
                thread = {
                    "id": f"psid:{peer_id}",
                    "peer_id": peer_id,
                    "peer_name": "Persona",
                    "updated_time": _now_iso(),
                    "messages": [],
                }
                threads.append(thread)
            mid = str(message.get("mid") or "")
            if mid and any(item.get("id") == mid for item in thread.get("messages") or []):
                continue
            thread.setdefault("messages", []).append(
                {
                    "id": mid or str(uuid.uuid4()),
                    "text": text,
                    "created_time": _now_iso(),
                    "own": own,
                    "author_name": "Página" if own else thread.get("peer_name") or "Persona",
                }
            )
            thread["messages"] = thread["messages"][-300:]
            thread["updated_time"] = _now_iso()
            data["threads"] = threads[:80]
            _save_messenger(data)
            stored += 1
    return stored


async def reply_messenger(thread_id: str, message: str) -> dict[str, Any]:
    conn = _require_connection()
    text = (message or "").strip()
    if not text:
        raise InstagramError("El mensaje no puede estar vacío")
    thread = get_messenger_thread(thread_id)
    peer_id = str(thread.get("peer_id") or "")
    if not peer_id:
        raise InstagramError("Falta el destinatario de Messenger")
    result = await _graph_post(
        f"/{conn['page_id']}/messages",
        conn["page_access_token"],
        {
            "recipient": json.dumps({"id": peer_id}),
            "message": json.dumps({"text": text}),
            "messaging_type": "RESPONSE",
        },
    )
    thread.setdefault("messages", []).append(
        {
            "id": result.get("message_id") or str(uuid.uuid4()),
            "text": text,
            "created_time": _now_iso(),
            "own": True,
            "author_name": "Página",
        }
    )
    thread["updated_time"] = _now_iso()
    _upsert_thread(thread)
    return thread


def refuse_messenger_delete() -> None:
    raise InstagramError(
        "Messenger no permite borrar un mensaje privado desde la página. "
        "Solo se puede borrar dentro de Facebook o Messenger."
    )
