"""Instagram Graph API: OAuth, inbox store, comments/DMs, auto-reply pipeline."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.core.database import SessionLocal
from app.services.deepseek_instagram import (
    DeepSeekConfigError,
    DeepSeekError,
    deepseek_configured,
    generate_instagram_reply,
)

CONNECTION_FILE = settings.uploads_dir / "cache" / "instagram_connection.json"
INBOX_FILE = settings.uploads_dir / "cache" / "instagram_inbox.json"
AUTO_REPLY_FILE = settings.uploads_dir / "cache" / "deepseek_auto_reply.json"

SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    "pages_read_user_content",
    "pages_manage_posts",
    "pages_manage_engagement",
    "instagram_basic",
    "instagram_manage_comments",
    "instagram_manage_messages",
    "pages_manage_metadata",
    "business_management",
]

MESSAGE_WEBHOOK_FIELDS = (
    "messages",
    "messaging_postbacks",
    "message_reads",
    "messaging_referrals",
    "message_reactions",
)


class InstagramConfigError(Exception):
    pass


class InstagramError(Exception):
    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.details = details


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dirs() -> None:
    CONNECTION_FILE.parent.mkdir(parents=True, exist_ok=True)


def _graph_base() -> str:
    version = (settings.graph_api_version or "v21.0").strip() or "v21.0"
    return f"https://graph.facebook.com/{version}"


def meta_configured() -> bool:
    return bool(
        (settings.meta_app_id or "").strip()
        and (settings.meta_app_secret or "").strip()
    )


def load_connection() -> dict[str, Any] | None:
    _ensure_dirs()
    if not CONNECTION_FILE.exists():
        return None
    try:
        data = json.loads(CONNECTION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("page_access_token") else None


def save_connection(data: dict[str, Any]) -> None:
    _ensure_dirs()
    CONNECTION_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def clear_connection() -> None:
    _ensure_dirs()
    if CONNECTION_FILE.exists():
        CONNECTION_FILE.unlink()


def _load_inbox_raw() -> dict[str, Any]:
    _ensure_dirs()
    if not INBOX_FILE.exists():
        return {"items": []}
    try:
        data = json.loads(INBOX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": []}
    if not isinstance(data, dict):
        return {"items": []}
    if not isinstance(data.get("items"), list):
        data["items"] = []
    return data


def _save_inbox_raw(data: dict[str, Any]) -> None:
    _ensure_dirs()
    INBOX_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_inbox(
    *,
    item_type: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    items = list(_load_inbox_raw().get("items") or [])
    if item_type:
        items = [i for i in items if i.get("type") == item_type]
    if status:
        items = [i for i in items if i.get("status") == status]
    items.sort(key=lambda i: i.get("created_at") or "", reverse=True)
    return items


def get_inbox_item(item_id: str) -> dict[str, Any] | None:
    for item in _load_inbox_raw().get("items") or []:
        if item.get("id") == item_id:
            return item
    return None


def upsert_inbox_item(item: dict[str, Any]) -> dict[str, Any]:
    data = _load_inbox_raw()
    items = data.get("items") or []
    external_id = item.get("external_id")
    for idx, existing in enumerate(items):
        if external_id and existing.get("external_id") == external_id:
            merged = {**existing, **item, "id": existing.get("id") or item.get("id")}
            items[idx] = merged
            data["items"] = items
            _save_inbox_raw(data)
            return merged
        if existing.get("id") == item.get("id"):
            merged = {**existing, **item}
            items[idx] = merged
            data["items"] = items
            _save_inbox_raw(data)
            return merged
    if not item.get("id"):
        item["id"] = str(uuid.uuid4())
    items.insert(0, item)
    items.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    data["items"] = items[:5000]
    _save_inbox_raw(data)
    return item


def find_by_external_id(external_id: str) -> dict[str, Any] | None:
    if not external_id:
        return None
    for item in _load_inbox_raw().get("items") or []:
        if item.get("external_id") == external_id:
            return item
    return None


def author_channel_history(
    *,
    author_username: str | None = None,
    author_id: str | None = None,
    exclude_id: str | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Other Instagram comments and DMs from the same person."""
    username = str(author_username or "").strip().lower().lstrip("@")
    author = str(author_id or "").strip()
    comments: list[dict[str, str]] = []
    messages: list[dict[str, str]] = []
    if not username and not author:
        return {"comments": comments, "messages": messages}
    for item in list_inbox():
        if exclude_id and item.get("id") == exclude_id:
            continue
        item_user = str(item.get("author_username") or "").strip().lower().lstrip("@")
        item_author = str(item.get("author_id") or "").strip()
        same_user = bool(username and item_user and username == item_user)
        same_id = bool(author and item_author and author == item_author)
        if not same_user and not same_id:
            continue
        row = {"text": str(item.get("text") or "").strip()[:180]}
        if item.get("type") == "comment":
            comments.append(row)
        elif item.get("type") == "message":
            messages.append(row)
    return {"comments": comments[:6], "messages": messages[:6]}


def auto_reply_enabled() -> bool:
    try:
        if AUTO_REPLY_FILE.exists():
            data = json.loads(AUTO_REPLY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "enabled" in data:
                return bool(data["enabled"])
    except (OSError, json.JSONDecodeError):
        pass
    return bool(settings.instagram_auto_reply_enabled)


def set_auto_reply_enabled(enabled: bool) -> bool:
    _ensure_dirs()
    value = bool(enabled)
    AUTO_REPLY_FILE.write_text(
        json.dumps({"enabled": value}, ensure_ascii=False),
        encoding="utf-8",
    )
    return value


def update_inbox_message(item_id: str, text: str) -> dict[str, Any]:
    raise InstagramError("Solo se editan las respuestas del hostal, y el cambio se publica en Instagram")


def _meta_already_gone(exc: InstagramError) -> bool:
    message = str(exc).lower()
    return any(
        part in message
        for part in (
            "does not exist",
            "nonexisting",
            "no existe",
            "unsupported delete request",
            "has been deleted",
        )
    )


async def _delete_on_instagram(object_id: str) -> None:
    target = str(object_id or "").strip()
    if not target:
        raise InstagramError("Falta el id para borrar el comentario en Instagram")
    conn = _require_connection()
    try:
        await _graph_delete(f"/{target}", conn["page_access_token"])
    except InstagramError as exc:
        if not _meta_already_gone(exc):
            raise


async def _instagram_reply_id(item: dict[str, Any], reply: dict[str, Any]) -> str | None:
    stored = str(reply.get("meta_message_id") or reply.get("meta_comment_id") or "").strip()
    if stored:
        return stored
    parent = str(item.get("meta_comment_id") or "").strip()
    if item.get("type") != "comment" or not parent:
        return None
    conn = _require_connection()
    data = await _graph_get(
        f"/{parent}/replies",
        conn["page_access_token"],
        {"fields": "id,text", "limit": 50},
    )
    wanted = str(reply.get("text") or "").strip()
    for row in reversed(data.get("data") or []):
        if str(row.get("text") or "").strip() == wanted and row.get("id"):
            return str(row["id"])
    return None


def _require_comment_item(item: dict[str, Any], action: str) -> None:
    if item.get("type") == "message":
        raise InstagramError(
            f"Instagram no permite {action} un mensaje privado. Solo se puede {action} un comentario."
        )
    if item.get("type") != "comment":
        raise InstagramError(f"No se puede {action} este elemento en Instagram")


async def delete_inbox_item(item_id: str) -> None:
    item = get_inbox_item(item_id)
    if not item:
        raise InstagramError("Item de inbox no encontrado")
    _require_comment_item(item, "borrar")
    for reply in list(item.get("replies") or []):
        reply_id = await _instagram_reply_id(item, reply)
        if reply_id:
            await _delete_on_instagram(reply_id)
    await _delete_on_instagram(str(item.get("meta_comment_id") or ""))
    data = _load_inbox_raw()
    items = list(data.get("items") or [])
    kept = [row for row in items if row.get("id") != item_id]
    if len(kept) == len(items):
        raise InstagramError("Item de inbox no encontrado")
    data["items"] = kept
    _save_inbox_raw(data)


async def update_inbox_reply(item_id: str, reply_index: int, text: str) -> dict[str, Any]:
    item = get_inbox_item(item_id)
    if not item:
        raise InstagramError("Item de inbox no encontrado")
    _require_comment_item(item, "editar")
    replies = list(item.get("replies") or [])
    if reply_index < 0 or reply_index >= len(replies):
        raise InstagramError("Respuesta no encontrada")
    cleaned = (text or "").strip()
    if not cleaned:
        raise InstagramError("La respuesta no puede quedar vacía")
    parent = str(item.get("meta_comment_id") or "").strip()
    if not parent:
        raise InstagramError("Falta el id del comentario para editarlo en Instagram")
    old_id = await _instagram_reply_id(item, replies[reply_index])
    posted = await reply_to_comment(parent, cleaned)
    new_id = str(posted.get("id") or "")
    if old_id and old_id != new_id:
        await _delete_on_instagram(old_id)
    replies[reply_index] = {
        **replies[reply_index],
        "text": cleaned,
        "meta_message_id": new_id,
        "updated_at": _now_iso(),
    }
    item["replies"] = replies
    item["updated_at"] = _now_iso()
    return upsert_inbox_item(item)


async def delete_inbox_reply(item_id: str, reply_index: int) -> dict[str, Any]:
    item = get_inbox_item(item_id)
    if not item:
        raise InstagramError("Item de inbox no encontrado")
    _require_comment_item(item, "borrar")
    replies = list(item.get("replies") or [])
    if reply_index < 0 or reply_index >= len(replies):
        raise InstagramError("Respuesta no encontrada")
    reply_id = await _instagram_reply_id(item, replies[reply_index])
    if not reply_id:
        raise InstagramError("No encuentro esa respuesta en Instagram para borrarla")
    await _delete_on_instagram(reply_id)
    replies.pop(reply_index)
    item["replies"] = replies
    item["updated_at"] = _now_iso()
    if not replies and item.get("status") in {"replied_human", "replied_deepseek"}:
        item["status"] = "pending"
    return upsert_inbox_item(item)


def connection_status() -> dict[str, Any]:
    conn = load_connection()
    return {
        "meta_configured": meta_configured(),
        "deepseek_configured": deepseek_configured(),
        "auto_reply_enabled": auto_reply_enabled(),
        "connected": bool(conn),
        "page_id": (conn or {}).get("page_id"),
        "page_name": (conn or {}).get("page_name"),
        "ig_user_id": (conn or {}).get("ig_user_id"),
        "ig_username": (conn or {}).get("ig_username"),
        "connected_at": (conn or {}).get("connected_at"),
        "hint": None
        if meta_configured()
        else "Configura META_APP_ID y META_APP_SECRET en backend/.env",
    }


def build_auth_url(state: str | None = None) -> str:
    if not meta_configured():
        raise InstagramConfigError(
            "Meta no está configurado. Agrega META_APP_ID y META_APP_SECRET en .env"
        )
    params = {
        "client_id": settings.meta_app_id.strip(),
        "redirect_uri": settings.meta_redirect_uri.strip(),
        "scope": ",".join(SCOPES),
        "response_type": "code",
        "state": state or str(uuid.uuid4()),
    }
    return f"https://www.facebook.com/{settings.graph_api_version}/dialog/oauth?{urlencode(params)}"


async def _graph_pages(
    path: str,
    token: str,
    params: dict[str, Any] | None = None,
    *,
    max_pages: int = 25,
) -> list[dict[str, Any]]:
    """Follow Graph paging.next so older records are not dropped."""
    collected: list[dict[str, Any]] = []
    next_url: str | None = None
    for _ in range(max_pages):
        if next_url:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.get(next_url)
            data = response.json() if response.content else {}
            if not isinstance(data, dict):
                break
            if response.status_code >= 400 or data.get("error"):
                break
        else:
            data = await _graph_get(path, token, params)
        collected.extend(data.get("data") or [])
        next_url = ((data.get("paging") or {}).get("next") or "").strip() or None
        if not next_url:
            break
    return collected


async def _graph_get(path: str, token: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = dict(params or {})
    query["access_token"] = token
    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.get(f"{_graph_base()}{path}", params=query)
    data = response.json() if response.content else {}
    if response.status_code >= 400 or data.get("error"):
        err = data.get("error") or {}
        raise InstagramError(
            err.get("message") or f"Graph GET {path} falló ({response.status_code})",
            details=data,
        )
    return data


async def _graph_delete(path: str, token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.delete(
            f"{_graph_base()}{path}",
            params={"access_token": token},
        )
    data = response.json() if response.content else {}
    if not isinstance(data, dict):
        data = {}
    if response.status_code >= 400 or data.get("error"):
        err = data.get("error") or {}
        raise InstagramError(
            err.get("message") or f"Graph DELETE {path} falló ({response.status_code})",
            details=data,
        )
    return data


async def _graph_post(path: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = {**payload, "access_token": token}
    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(f"{_graph_base()}{path}", data=body)
    data = response.json() if response.content else {}
    if response.status_code >= 400 or data.get("error"):
        err = data.get("error") or {}
        raise InstagramError(
            err.get("message") or f"Graph POST {path} falló ({response.status_code})",
            details=data,
        )
    return data


async def exchange_code_and_connect(code: str) -> dict[str, Any]:
    if not meta_configured():
        raise InstagramConfigError("Meta no configurado")

    async with httpx.AsyncClient(timeout=45.0) as client:
        token_resp = await client.get(
            f"{_graph_base()}/oauth/access_token",
            params={
                "client_id": settings.meta_app_id.strip(),
                "client_secret": settings.meta_app_secret.strip(),
                "redirect_uri": settings.meta_redirect_uri.strip(),
                "code": code,
            },
        )
        short = token_resp.json()
        if token_resp.status_code >= 400 or short.get("error"):
            raise InstagramError("No se pudo canjear el code OAuth", details=short)
        user_token = short.get("access_token")
        if not user_token:
            raise InstagramError("Token de usuario vacío", details=short)

        long_resp = await client.get(
            f"{_graph_base()}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": settings.meta_app_id.strip(),
                "client_secret": settings.meta_app_secret.strip(),
                "fb_exchange_token": user_token,
            },
        )
        long_data = long_resp.json()
        long_token = long_data.get("access_token") or user_token

    pages = await _graph_get(
        "/me/accounts",
        long_token,
        {
            "fields": "id,name,access_token,instagram_business_account{id,username}",
        },
    )
    page_list = pages.get("data") or []
    chosen = None
    for page in page_list:
        if page.get("instagram_business_account"):
            chosen = page
            break
    if not chosen and page_list:
        chosen = page_list[0]
    if not chosen:
        raise InstagramError(
            "No se encontró una Facebook Page. Vincula Instagram Business a una Page."
        )

    ig = chosen.get("instagram_business_account") or {}
    if not ig.get("id"):
        raise InstagramError(
            "La Page no tiene Instagram Business Account vinculado.",
            details=chosen,
        )

    conn = {
        "page_id": chosen.get("id"),
        "page_name": chosen.get("name"),
        "page_access_token": chosen.get("access_token"),
        "user_access_token": long_token,
        "ig_user_id": ig.get("id"),
        "ig_username": ig.get("username"),
        "connected_at": _now_iso(),
        "token_expires_at": None,
    }
    save_connection(conn)
    await subscribe_instagram_webhooks(conn)
    return conn


async def subscribe_instagram_webhooks(conn: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ask Meta to deliver Instagram DMs for this Page, not only comments."""
    conn = conn or _require_connection()
    page_id = conn.get("page_id")
    token = conn.get("page_access_token")
    if not page_id or not token:
        return {"ok": False, "error": "Falta la página de Facebook conectada."}
    try:
        result = await _graph_post(
            f"/{page_id}/subscribed_apps",
            token,
            {"subscribed_fields": ",".join(MESSAGE_WEBHOOK_FIELDS)},
        )
        return {"ok": True, "result": result}
    except InstagramError as exc:
        return {"ok": False, "error": str(exc)}


def _require_connection() -> dict[str, Any]:
    conn = load_connection()
    if not conn:
        raise InstagramConfigError("Instagram no está conectado. Usa Conectar Instagram.")
    return conn


async def reply_to_comment(comment_id: str, message: str) -> dict[str, Any]:
    conn = _require_connection()
    return await _graph_post(
        f"/{comment_id}/replies",
        conn["page_access_token"],
        {"message": message},
    )


async def send_dm(recipient_id: str, message: str) -> dict[str, Any]:
    conn = _require_connection()
    page_id = conn["page_id"]
    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(
            f"{_graph_base()}/{page_id}/messages",
            params={"access_token": conn["page_access_token"]},
            json={
                "recipient": {"id": recipient_id},
                "message": {"text": message},
            },
        )
    data = response.json() if response.content else {}
    if response.status_code >= 400 or data.get("error"):
        err = data.get("error") or {}
        raise InstagramError(
            err.get("message") or "No se pudo enviar el DM",
            details=data,
        )
    return data


async def sync_recent_comments(limit_media: int = 8) -> list[dict[str, Any]]:
    """Pull recent media comments into local inbox (best-effort)."""
    conn = _require_connection()
    ig_user_id = conn["ig_user_id"]
    token = conn["page_access_token"]
    media = await _graph_get(
        f"/{ig_user_id}/media",
        token,
        {"fields": "id,caption,timestamp,permalink", "limit": limit_media},
    )
    synced: list[dict[str, Any]] = []
    for m in media.get("data") or []:
        media_id = m.get("id")
        if not media_id:
            continue
        try:
            comments = await _graph_get(
                f"/{media_id}/comments",
                token,
                {
                    "fields": "id,text,username,timestamp,from",
                    "limit": 50,
                },
            )
        except InstagramError:
            continue
        for c in comments.get("data") or []:
            external_id = f"comment:{c.get('id')}"
            if find_by_external_id(external_id):
                continue
            from_user = c.get("from") or {}
            item = upsert_inbox_item(
                {
                    "id": str(uuid.uuid4()),
                    "type": "comment",
                    "thread_id": media_id,
                    "external_id": external_id,
                    "meta_comment_id": c.get("id"),
                    "media_id": media_id,
                    "author_username": c.get("username") or from_user.get("username"),
                    "author_id": from_user.get("id"),
                    "text": c.get("text") or "",
                    "created_at": c.get("timestamp") or _now_iso(),
                    "status": "pending",
                    "replies": [],
                    "escalation_reason": None,
                    "permalink": m.get("permalink"),
                }
            )
            synced.append(item)
    return synced


async def sync_recent_messages(limit_conversations: int = 50) -> list[dict[str, Any]]:
    """Pull Instagram DM history, including older pages. Does not auto-reply."""
    conn = _require_connection()
    token = conn["page_access_token"]
    ig_user_id = str(conn.get("ig_user_id") or "")
    page_id = str(conn.get("page_id") or "")
    ig_username = str(conn.get("ig_username") or "").lower()
    params = {
        "platform": "instagram",
        "fields": "id,updated_time,participants",
        "limit": max(1, min(limit_conversations, 50)),
    }
    conversations: list[dict[str, Any]] | None = None
    last_error = "No se pudieron leer los mensajes privados"
    for path in (f"/{ig_user_id}/conversations", f"/{page_id}/conversations"):
        if path == "/conversations":
            continue
        try:
            conversations = await _graph_pages(path, token, params, max_pages=40)
            break
        except InstagramError as exc:
            last_error = str(exc)
    if conversations is None:
        raise InstagramError(last_error)

    own_ids = {item for item in (ig_user_id, page_id) if item}
    synced: list[dict[str, Any]] = []
    for conv in conversations:
        thread_id = str(conv.get("id") or "")
        names = {
            str(person.get("id") or ""): person.get("username") or person.get("name") or ""
            for person in ((conv.get("participants") or {}).get("data") or [])
            if person.get("id")
        }
        if not thread_id:
            continue
        try:
            messages = await _graph_pages(
                f"/{thread_id}/messages",
                token,
                {"fields": "id,message,from,created_time", "limit": 50},
                max_pages=20,
            )
        except InstagramError:
            messages = ((conv.get("messages") or {}).get("data")) or []
        for msg in messages:
            sender = msg.get("from") or {}
            sender_id = str(sender.get("id") or "")
            username = str(sender.get("username") or names.get(sender_id) or "")
            if not sender_id or sender_id in own_ids or username.lower() == ig_username:
                continue
            external_id = f"message:{msg.get('id') or uuid.uuid4()}"
            if find_by_external_id(external_id):
                continue
            text = str(msg.get("message") or "").strip() or "[archivo]"
            item = upsert_inbox_item(
                {
                    "id": str(uuid.uuid4()),
                    "type": "message",
                    "thread_id": thread_id or sender_id,
                    "external_id": external_id,
                    "meta_comment_id": None,
                    "media_id": None,
                    "author_username": username or None,
                    "author_id": sender_id,
                    "text": text,
                    "created_at": msg.get("created_time") or _now_iso(),
                    "status": "pending",
                    "replies": [],
                    "escalation_reason": None,
                }
            )
            synced.append(item)
    return synced


async def send_inbox_reply(item_id: str, message: str, *, source: str = "human") -> dict[str, Any]:
    item = get_inbox_item(item_id)
    if not item:
        raise InstagramError("Item de inbox no encontrado")
    text = (message or "").strip()
    if not text:
        raise InstagramError("Mensaje vacío")

    meta_result: dict[str, Any]
    if item.get("type") == "comment":
        comment_id = item.get("meta_comment_id")
        if not comment_id:
            raise InstagramError("Falta meta_comment_id")
        meta_result = await reply_to_comment(comment_id, text)
    else:
        recipient_id = item.get("author_id")
        if not recipient_id:
            raise InstagramError("Falta author_id (IGSID) para DM")
        meta_result = await send_dm(recipient_id, text)

    reply = {
        "text": text,
        "source": source,
        "created_at": _now_iso(),
        "meta_message_id": meta_result.get("id") or meta_result.get("message_id"),
    }
    replies = list(item.get("replies") or [])
    replies.append(reply)
    item["replies"] = replies
    item["status"] = "replied_deepseek" if source == "deepseek" else "replied_human"
    item["updated_at"] = _now_iso()
    return upsert_inbox_item(item)


async def run_auto_reply(item_id: str) -> dict[str, Any]:
    item = get_inbox_item(item_id)
    if not item:
        raise InstagramError("Item de inbox no encontrado")

    if item.get("type") == "message":
        created = item.get("created_at")
        # Soft 24h check if timestamp parseable as unix-ish ISO; skip if unknown
        try:
            if created and "T" in str(created):
                # leave as soft; Meta enforces window
                pass
        except Exception:
            pass

    try:
        db = SessionLocal()
        try:
            decision = await generate_instagram_reply(
                db=db,
                inbound_text=item.get("text") or "",
                item_type=item.get("type") or "message",
                author_username=item.get("author_username"),
                person_history=author_channel_history(
                    author_username=item.get("author_username"),
                    author_id=item.get("author_id"),
                    exclude_id=item.get("id"),
                ),
            )
        finally:
            db.close()
    except (DeepSeekConfigError, DeepSeekError) as exc:
        item["status"] = "failed"
        item["escalation_reason"] = str(exc)
        item["updated_at"] = _now_iso()
        upsert_inbox_item(item)
        raise

    if decision["action"] == "escalate":
        item["status"] = "needs_human"
        item["escalation_reason"] = decision.get("reason") or "escalate"
        if decision.get("message"):
            # optional acknowledgment not auto-sent on escalate
            item["deepseek_draft"] = decision["message"]
        item["updated_at"] = _now_iso()
        return upsert_inbox_item(item)

    return await send_inbox_reply(item_id, decision["message"], source="deepseek")


async def suggest_reply(item_id: str) -> dict[str, Any]:
    item = get_inbox_item(item_id)
    if not item:
        raise InstagramError("Item de inbox no encontrado")
    db = SessionLocal()
    try:
        decision = await generate_instagram_reply(
            db=db,
            inbound_text=item.get("text") or "",
            item_type=item.get("type") or "message",
            author_username=item.get("author_username"),
            person_history=author_channel_history(
                author_username=item.get("author_username"),
                author_id=item.get("author_id"),
                exclude_id=item.get("id"),
            ),
        )
    finally:
        db.close()
    item["deepseek_draft"] = decision.get("message") or ""
    item["deepseek_action"] = decision.get("action")
    item["escalation_reason"] = decision.get("reason")
    item["updated_at"] = _now_iso()
    upsert_inbox_item(item)
    return decision


def ingest_inbound(
    *,
    item_type: str,
    external_id: str,
    text: str,
    author_id: str | None = None,
    author_username: str | None = None,
    thread_id: str | None = None,
    meta_comment_id: str | None = None,
    media_id: str | None = None,
) -> dict[str, Any]:
    existing = find_by_external_id(external_id)
    if existing:
        return existing
    return upsert_inbox_item(
        {
            "id": str(uuid.uuid4()),
            "type": item_type,
            "thread_id": thread_id or author_id or external_id,
            "external_id": external_id,
            "meta_comment_id": meta_comment_id,
            "media_id": media_id,
            "author_username": author_username,
            "author_id": author_id,
            "text": text or "",
            "created_at": _now_iso(),
            "status": "pending",
            "replies": [],
            "escalation_reason": None,
        }
    )


def _own_instagram_ids() -> set[str]:
    conn = load_connection() or {}
    return {str(item) for item in (conn.get("ig_user_id"), conn.get("page_id")) if item}


def _dm_event_text(event: dict[str, Any]) -> tuple[str, str, str]:
    """Return (sender_id, mid, text) for an Instagram DM webhook event."""
    sender = str((event.get("sender") or {}).get("id") or "")
    message = event.get("message")
    if isinstance(message, dict):
        if message.get("is_echo") or message.get("is_deleted") or sender in _own_instagram_ids():
            return "", "", ""
        mid = str(message.get("mid") or "")
        text = str(message.get("text") or "").strip()
        if not text and message.get("attachments"):
            text = "[archivo]"
        return sender, mid, text
    postback = event.get("postback") or {}
    if isinstance(postback, dict) and sender and sender not in _own_instagram_ids():
        text = str(postback.get("title") or postback.get("payload") or "").strip()
        return sender, str(postback.get("mid") or ""), text
    return "", "", ""


def _known_username(author_id: str) -> str | None:
    for item in list_inbox():
        if str(item.get("author_id") or "") == author_id and item.get("author_username"):
            return str(item["author_username"])
    return None


async def _instagram_username(author_id: str) -> str | None:
    known = _known_username(author_id)
    if known:
        return known
    conn = load_connection()
    if not conn:
        return None
    try:
        profile = await _graph_get(
            f"/{author_id}",
            conn["page_access_token"],
            {"fields": "username"},
        )
    except InstagramError:
        return None
    username = str(profile.get("username") or "").strip()
    return username or None


async def _store_dm_event(event: dict[str, Any]) -> dict[str, Any] | None:
    sender, mid, text = _dm_event_text(event)
    if not sender or not text:
        return None
    username = await _instagram_username(sender)
    item = ingest_inbound(
        item_type="message",
        external_id=f"message:{mid or f'{sender}-{int(time.time())}'}",
        text=text,
        author_id=sender,
        author_username=username,
        thread_id=sender,
    )
    if username and not item.get("author_username"):
        item["author_username"] = username
        item = upsert_inbox_item(item)
    if auto_reply_enabled() and item.get("status") == "pending":
        try:
            item = await run_auto_reply(item["id"])
        except Exception as exc:  # noqa: BLE001 — keep webhook 200
            item["status"] = "failed"
            item["escalation_reason"] = str(exc)
            upsert_inbox_item(item)
    return item


async def process_webhook_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse Meta webhook and auto-reply when enabled."""
    results: list[dict[str, Any]] = []
    dm_fields = {"messages", "message_echoes", "messaging_postbacks"}
    for entry in payload.get("entry") or []:
        for messaging in (entry.get("messaging") or []) + (entry.get("standby") or []):
            item = await _store_dm_event(messaging)
            if item:
                results.append(item)

        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            field = str(change.get("field") or "")
            if field in dm_fields or (
                isinstance(value.get("message"), dict) and value.get("sender")
            ):
                item = await _store_dm_event(value)
                if item:
                    results.append(item)
                continue
            if field not in {"comments", "mentions", "live_comments"} and "text" not in value:
                continue
            comment_id = value.get("id") or value.get("comment_id")
            text = value.get("text") or ""
            if isinstance(text, dict):
                continue
            from_user = value.get("from") or {}
            media = value.get("media") or {}
            if not comment_id and not str(text).strip():
                continue
            external = f"comment:{comment_id or uuid.uuid4()}"
            item = ingest_inbound(
                item_type="comment",
                external_id=external,
                text=str(text),
                author_id=from_user.get("id"),
                author_username=from_user.get("username"),
                thread_id=media.get("id") or value.get("media_id"),
                meta_comment_id=comment_id,
                media_id=media.get("id") or value.get("media_id"),
            )
            if auto_reply_enabled() and item.get("status") == "pending":
                try:
                    item = await run_auto_reply(item["id"])
                except Exception as exc:  # noqa: BLE001
                    item["status"] = "failed"
                    item["escalation_reason"] = str(exc)
                    upsert_inbox_item(item)
            results.append(item)
    return results
