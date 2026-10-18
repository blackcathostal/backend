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
        "deepseek_configured": data.get("deepseek_configured"),
        "auto_reply_enabled": data.get("auto_reply_enabled"),
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


INBOX_FILE = settings.uploads_dir / "cache" / "facebook_inbox.json"


def _load_inbox() -> dict[str, Any]:
    INBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not INBOX_FILE.exists():
        return {"items": []}
    try:
        data = json.loads(INBOX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": []}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"items": []}
    return data


def _save_inbox(data: dict[str, Any]) -> None:
    INBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    INBOX_FILE.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _comment_item(comment: dict[str, Any], post_id: str, replies: list[dict[str, Any]]) -> dict[str, Any]:
    name = str(comment.get("author_name") or "").strip()
    return {
        "id": comment.get("id") or "",
        "type": "comment",
        "post_id": post_id,
        "thread_id": post_id,
        "author_username": name,
        "author_id": comment.get("author_id") or "",
        "peer_id": comment.get("author_id") or "",
        "peer_username": name,
        "direction": "in",
        "text": comment.get("message") or "",
        "created_at": comment.get("created_time") or "",
        "status": "replied_human" if replies else "pending",
        "replies": replies,
        "escalation_reason": None,
    }


def _merge_comment_items(fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    data = _load_inbox()
    by_id = {str(item.get("id") or ""): item for item in data.get("items") or [] if item.get("id")}
    for item in fresh:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        old = by_id.get(item_id) or {}
        if old.get("status") in {"replied_deepseek", "needs_human", "failed"} and item.get("status") == "replied_human":
            item["status"] = old["status"]
        if old.get("status") in {"replied_deepseek", "needs_human", "failed"} and item.get("status") == "pending" and old.get("replies"):
            item["status"] = old["status"]
            item["replies"] = old.get("replies") or item.get("replies")
        if old.get("deepseek_draft"):
            item["deepseek_draft"] = old.get("deepseek_draft")
        if old.get("escalation_reason") and not item.get("escalation_reason"):
            item["escalation_reason"] = old.get("escalation_reason")
        by_id[item_id] = item
    items = [item for item in by_id.values() if item.get("type") == "comment"]
    items.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    data["items"] = items[:800]
    _save_inbox(data)
    return data["items"]


async def sync_page_comments(limit_posts: int = 15) -> list[dict[str, Any]]:
    """Store guest comments from recent Page posts. Page replies stay inside each comment."""
    posts = await list_posts(limit=limit_posts)
    fresh: list[dict[str, Any]] = []
    for post in posts:
        post_id = str(post.get("id") or "")
        if not post_id:
            continue
        payload = await list_comments(post_id)
        replies_by_parent: dict[str, list[dict[str, Any]]] = {}
        guests: list[dict[str, Any]] = []
        for comment in payload.get("comments") or []:
            if comment.get("own"):
                parent = str(comment.get("parent_id") or "")
                if not parent:
                    continue
                replies_by_parent.setdefault(parent, []).append(
                    {
                        "text": comment.get("message") or "",
                        "created_at": comment.get("created_time") or "",
                        "source": "human",
                        "meta_comment_id": comment.get("id") or "",
                    }
                )
            else:
                guests.append(comment)
        for comment in guests:
            comment_id = str(comment.get("id") or "")
            replies = replies_by_parent.get(comment_id, [])
            fresh.append(_comment_item(comment, post_id, replies))
    return _merge_comment_items(fresh)


def _message_row(thread: dict[str, Any]) -> dict[str, Any]:
    messages = list(thread.get("messages") or [])
    last = messages[-1] if messages else {}
    own = bool(last.get("own"))
    name = str(thread.get("peer_name") or "").strip()
    text = str(last.get("text") or thread.get("last_message") or "").strip()
    return {
        "id": thread.get("id") or "",
        "type": "message",
        "thread_id": thread.get("id") or "",
        "author_username": name,
        "author_id": thread.get("peer_id") or "",
        "peer_id": thread.get("peer_id") or "",
        "peer_username": name,
        "direction": "out" if own else "in",
        "text": text[:180],
        "created_at": thread.get("updated_time") or last.get("created_time") or "",
        "status": "replied_human" if own else "pending",
        "message_count": len(messages) or 1,
    }


def list_inbox_page(*, item_type: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if item_type in {None, "", "comment"}:
        for item in _load_inbox().get("items") or []:
            if item.get("type") != "comment":
                continue
            text = str(item.get("text") or "").strip()
            if len(text) > 180:
                text = text[:177].rstrip() + "..."
            rows.append(
                {
                    "id": item.get("id"),
                    "type": "comment",
                    "thread_id": item.get("thread_id"),
                    "author_username": item.get("author_username"),
                    "author_id": item.get("author_id"),
                    "peer_id": item.get("peer_id"),
                    "peer_username": item.get("peer_username"),
                    "direction": item.get("direction") or "in",
                    "text": text,
                    "created_at": item.get("created_at"),
                    "status": item.get("status") or "pending",
                    "message_count": 1,
                }
            )
    if item_type in {None, "", "message"}:
        for thread in _load_messenger().get("threads") or []:
            if thread.get("id"):
                rows.append(_message_row(thread))
    if status:
        rows = [row for row in rows if row.get("status") == status]
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows


def _comment_by_id(item_id: str) -> dict[str, Any] | None:
    for item in _load_inbox().get("items") or []:
        if str(item.get("id") or "") == str(item_id):
            return item
    return None


def _save_comment(item: dict[str, Any]) -> dict[str, Any]:
    data = _load_inbox()
    items = list(data.get("items") or [])
    for index, existing in enumerate(items):
        if str(existing.get("id") or "") == str(item.get("id") or ""):
            items[index] = item
            data["items"] = items
            _save_inbox(data)
            return item
    items.insert(0, item)
    data["items"] = items[:800]
    _save_inbox(data)
    return item


def _conversation(item: dict[str, Any]) -> list[dict[str, Any]]:
    if item.get("type") == "message":
        thread = get_messenger_thread(str(item.get("id") or ""))
        turns = []
        for message in thread.get("messages") or []:
            own = bool(message.get("own"))
            turns.append(
                {
                    "role": "out" if own else "in",
                    "text": message.get("text") or "",
                    "created_at": message.get("created_time"),
                    "author": "Tú" if own else (message.get("author_name") or thread.get("peer_name") or ""),
                    "source": "human" if own else "",
                    "item_id": thread.get("id"),
                    "reply_index": None,
                }
            )
        return turns
    turns = [
        {
            "role": "in",
            "text": item.get("text") or "",
            "created_at": item.get("created_at"),
            "author": item.get("author_username") or "",
            "item_id": item.get("id"),
            "reply_index": None,
        }
    ]
    for index, reply in enumerate(item.get("replies") or []):
        turns.append(
            {
                "role": "out",
                "text": reply.get("text") or "",
                "created_at": reply.get("created_at"),
                "source": reply.get("source") or "human",
                "author": "DeepSeek" if reply.get("source") == "deepseek" else "Tú",
                "item_id": item.get("id"),
                "reply_index": index,
            }
        )
    return turns


def get_inbox_detail(item_id: str) -> dict[str, Any]:
    comment = _comment_by_id(item_id)
    if comment:
        payload = dict(comment)
        payload["conversation"] = _conversation(comment)
        return payload
    try:
        thread = get_messenger_thread(item_id)
    except InstagramError:
        thread = None
    if not thread:
        raise InstagramError("No encontrado")
    payload = _message_row(thread)
    payload["text"] = ""
    payload["deepseek_draft"] = thread.get("deepseek_draft") or ""
    payload["escalation_reason"] = thread.get("escalation_reason")
    payload["conversation"] = _conversation({"id": thread.get("id"), "type": "message"})
    if payload["conversation"]:
        payload["text"] = payload["conversation"][-1].get("text") or ""
    return payload


async def reply_inbox(item_id: str, message: str, *, source: str = "human") -> dict[str, Any]:
    text = (message or "").strip()
    if not text:
        raise InstagramError("El mensaje no puede estar vacío")
    comment = _comment_by_id(item_id)
    if comment:
        result = await reply_to_comment(str(comment.get("id") or ""), text)
        replies = list(comment.get("replies") or [])
        replies.append(
            {
                "text": text,
                "created_at": _now_iso(),
                "source": source,
                "meta_comment_id": result.get("id") or "",
            }
        )
        comment["replies"] = replies
        comment["status"] = "replied_deepseek" if source == "deepseek" else "replied_human"
        comment["updated_at"] = _now_iso()
        _save_comment(comment)
        return get_inbox_detail(item_id)
    await reply_messenger(item_id, text)
    return get_inbox_detail(item_id)


async def delete_inbox_item(item_id: str) -> None:
    comment = _comment_by_id(item_id)
    if not comment:
        refuse_messenger_delete()
    for reply in list(comment.get("replies") or []):
        reply_id = str(reply.get("meta_comment_id") or "").strip()
        if reply_id:
            try:
                await delete_comment(reply_id)
            except InstagramError:
                pass
    await delete_comment(str(comment.get("id") or ""))
    data = _load_inbox()
    data["items"] = [item for item in data.get("items") or [] if str(item.get("id") or "") != str(item_id)]
    _save_inbox(data)


async def update_inbox_reply(item_id: str, reply_index: int, message: str) -> dict[str, Any]:
    comment = _comment_by_id(item_id)
    if not comment:
        raise InstagramError("Messenger no permite editar un mensaje privado desde la página.")
    replies = list(comment.get("replies") or [])
    if reply_index < 0 or reply_index >= len(replies):
        raise InstagramError("Respuesta no encontrada")
    reply_id = str(replies[reply_index].get("meta_comment_id") or "").strip()
    if not reply_id:
        raise InstagramError("No encuentro esa respuesta en Facebook para editarla")
    updated = await update_comment(reply_id, message)
    replies[reply_index]["text"] = updated.get("message") or message
    replies[reply_index]["updated_at"] = _now_iso()
    comment["replies"] = replies
    _save_comment(comment)
    return get_inbox_detail(item_id)


async def delete_inbox_reply(item_id: str, reply_index: int) -> dict[str, Any]:
    comment = _comment_by_id(item_id)
    if not comment:
        refuse_messenger_delete()
    replies = list(comment.get("replies") or [])
    if reply_index < 0 or reply_index >= len(replies):
        raise InstagramError("Respuesta no encontrada")
    reply_id = str(replies[reply_index].get("meta_comment_id") or "").strip()
    if not reply_id:
        raise InstagramError("No encuentro esa respuesta en Facebook para borrarla")
    await delete_comment(reply_id)
    replies.pop(reply_index)
    comment["replies"] = replies
    if not replies and comment.get("status") in {"replied_human", "replied_deepseek"}:
        comment["status"] = "pending"
    _save_comment(comment)
    return get_inbox_detail(item_id)


def _inbound_text(item_id: str) -> tuple[dict[str, Any], str]:
    detail = get_inbox_detail(item_id)
    if detail.get("type") == "message":
        incoming = [turn for turn in detail.get("conversation") or [] if turn.get("role") == "in"]
        text = str((incoming[-1] if incoming else {}).get("text") or detail.get("text") or "")
        return detail, text
    return detail, str(detail.get("text") or "")


async def suggest_inbox_reply(item_id: str) -> dict[str, Any]:
    from app.core.database import SessionLocal
    from app.services.deepseek_instagram import generate_instagram_reply

    detail, text = _inbound_text(item_id)
    db = SessionLocal()
    try:
        decision = await generate_instagram_reply(
            db=db,
            inbound_text=text,
            item_type="comment" if detail.get("type") == "comment" else "message",
            author_username=detail.get("peer_username") or detail.get("author_username"),
        )
    finally:
        db.close()
    if detail.get("type") == "comment":
        comment = _comment_by_id(item_id) or {}
        comment["deepseek_draft"] = decision.get("message") or ""
        comment["escalation_reason"] = decision.get("reason")
        if decision.get("action") == "escalate":
            comment["status"] = "needs_human"
        _save_comment(comment)
    else:
        thread = get_messenger_thread(item_id)
        thread["deepseek_draft"] = decision.get("message") or ""
        thread["escalation_reason"] = decision.get("reason")
        _upsert_thread(thread)
    return decision


async def auto_inbox_reply(item_id: str) -> dict[str, Any]:
    decision = await suggest_inbox_reply(item_id)
    if decision.get("action") == "escalate":
        return get_inbox_detail(item_id)
    message = str(decision.get("message") or "").strip()
    if not message:
        raise InstagramError("DeepSeek no escribió una respuesta")
    return await reply_inbox(item_id, message, source="deepseek")


async def sync_inbox() -> dict[str, Any]:
    comments = await sync_page_comments()
    warning = None
    messages = 0
    try:
        result = await sync_messenger()
        messages = int(result.get("synced") or 0)
    except InstagramError as exc:
        warning = str(exc)
    return {
        "synced": len(comments) + messages,
        "comments": len(comments),
        "messages": messages,
        "warning": warning,
    }
