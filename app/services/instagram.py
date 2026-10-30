"""Instagram Graph API: OAuth, inbox store, comments/DMs, auto-reply pipeline."""

from __future__ import annotations

import json
import re
import threading
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
from app.services.short_links import ver_mapa_message

CONNECTION_FILE = settings.uploads_dir / "cache" / "instagram_connection.json"
INBOX_FILE = settings.uploads_dir / "cache" / "instagram_inbox.json"
AUTO_REPLY_FILE = settings.uploads_dir / "cache" / "deepseek_auto_reply.json"
_inbox_lock = threading.Lock()
_inbox_cache: dict[str, Any] | None = None

SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    "pages_read_user_content",
    "pages_manage_posts",
    "pages_manage_engagement",
    "instagram_basic",
    "instagram_manage_comments",
    "instagram_manage_messages",
    "instagram_content_publish",
    "pages_manage_metadata",
    "pages_messaging",
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


WINDOW_CLOSED_MESSAGE = (
    "La ventana de 24 horas ya cerró. Instagram/Messenger solo permite responder "
    "si la persona escribió en las últimas 24 horas. Cuando vuelva a escribir, "
    "ahí sí se puede contestar de nuevo."
)


def _meta_error_message(err: dict[str, Any], fallback: str) -> str:
    raw = str(err.get("message") or fallback or "").strip()
    code = err.get("code")
    sample = raw.lower()
    if (
        code in {10, 100}
        or "fuera del período" in sample
        or "outside of the allowed window" in sample
        or "outside the allowed window" in sample
        or "human agent" in sample
    ):
        return WINDOW_CLOSED_MESSAGE
    return raw or fallback


def _is_window_closed_error(exc: Exception) -> bool:
    text = str(exc).lower()
    if WINDOW_CLOSED_MESSAGE[:40].lower() in text:
        return True
    return (
        "fuera del período" in text
        or "outside of the allowed window" in text
        or "outside the allowed window" in text
    )


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
    global _inbox_cache
    with _inbox_lock:
        if _inbox_cache is not None:
            return _inbox_cache
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
    with _inbox_lock:
        _inbox_cache = data
    return data


def _save_inbox_raw(data: dict[str, Any]) -> None:
    global _inbox_cache
    _ensure_dirs()
    INBOX_FILE.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    with _inbox_lock:
        _inbox_cache = data


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


def _clean_handle(value: Any) -> str:
    name = str(value or "").strip().lstrip("@")
    if not name or (name.isdigit() and len(name) >= 6):
        return ""
    return name


def _is_own_message(item: dict[str, Any], own_ids: set[str], ig_username: str) -> bool:
    if item.get("direction") == "out":
        return True
    if str(item.get("author_id") or "") in own_ids:
        return True
    name = _clean_handle(item.get("author_username")).lower()
    return bool(ig_username and name and name == ig_username)


def _guest_id(item: dict[str, Any], own_ids: set[str]) -> str:
    author = str(item.get("author_id") or "")
    peer = str(item.get("peer_id") or "")
    if item.get("direction") == "out" or author in own_ids:
        return "" if not peer or peer in own_ids else peer
    if author and author not in own_ids:
        return author
    return "" if not peer or peer in own_ids else peer


def _group_dm_items(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """One bucket per conversation, matching thread id or guest id in a single pass."""
    own_ids = _own_instagram_ids()
    parent = list(range(len(messages)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    by_thread: dict[str, int] = {}
    by_guest: dict[str, int] = {}
    for index, item in enumerate(messages):
        thread = str(item.get("thread_id") or "")
        guest = _guest_id(item, own_ids)
        if thread:
            previous = by_thread.get(thread)
            if previous is not None:
                union(index, previous)
            by_thread[thread] = index
        if guest:
            previous = by_guest.get(guest)
            if previous is not None:
                union(index, previous)
            by_guest[guest] = index
    buckets: dict[int, list[dict[str, Any]]] = {}
    for index, item in enumerate(messages):
        buckets.setdefault(find(index), []).append(item)
    return list(buckets.values())


def _client_username(group: list[dict[str, Any]], own_ids: set[str], ig_username: str) -> str:
    for row in group:
        peer = _clean_handle(row.get("peer_username"))
        if peer:
            return peer
        if not _is_own_message(row, own_ids, ig_username):
            name = _clean_handle(row.get("author_username"))
            if name:
                return name
    return ""


def _page_row(item: dict[str, Any], *, message_count: int = 1, peer_username: str | None = None) -> dict[str, Any]:
    text = str(item.get("text") or "").strip()
    if len(text) > 180:
        text = text[:177].rstrip() + "..."
    return {
        "id": item.get("id"),
        "type": item.get("type"),
        "thread_id": item.get("thread_id"),
        "author_username": item.get("author_username"),
        "author_id": item.get("author_id"),
        "peer_id": item.get("peer_id"),
        "peer_username": peer_username if peer_username is not None else item.get("peer_username"),
        "direction": item.get("direction"),
        "text": text,
        "created_at": item.get("created_at"),
        "status": item.get("status"),
        "message_count": message_count,
    }


def list_inbox_page(
    *,
    item_type: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Slim rows for the admin table: one row per DM thread, comments unchanged."""
    items = list_inbox(item_type=item_type, status=status)
    comments = [item for item in items if item.get("type") != "message"]
    messages = [item for item in items if item.get("type") == "message"]
    own_ids = _own_instagram_ids()
    ig_username = str((load_connection() or {}).get("ig_username") or "").strip().lower()
    rows: list[dict[str, Any]] = []
    for group in _group_dm_items(messages):
        latest = max(group, key=lambda row: str(row.get("created_at") or ""))
        rows.append(
            _page_row(
                latest,
                message_count=len(group),
                peer_username=_client_username(group, own_ids, ig_username) or None,
            )
        )
    rows.extend(_page_row(item) for item in comments)
    rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return rows


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


def thread_history_for_reply(
    item: dict[str, Any],
    *,
    inbound_text: str | None = None,
    limit: int = 16,
) -> list[dict[str, str]]:
    """Prior user/assistant turns for DeepSeek, without the current inbound message."""
    current = (inbound_text if inbound_text is not None else item.get("text") or "").strip()
    turns: list[dict[str, str]] = []

    if item.get("type") == "message":
        for turn in conversation_turns(item):
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            role = "assistant" if turn.get("role") == "out" else "user"
            turns.append({"role": role, "content": text[:800]})
    else:
        username = str(item.get("author_username") or "").strip().lower().lstrip("@")
        author = str(item.get("author_id") or "").strip()
        dated: list[tuple[str, str, str]] = []
        for row in list_inbox():
            item_user = str(row.get("author_username") or "").strip().lower().lstrip("@")
            item_author = str(row.get("author_id") or "").strip()
            same_user = bool(username and item_user and username == item_user)
            same_id = bool(author and item_author and author == item_author)
            if not same_user and not same_id:
                continue
            stamp = str(row.get("created_at") or "")
            guest = str(row.get("text") or "").strip()
            if guest:
                dated.append((stamp, "user", guest[:800]))
            for reply in row.get("replies") or []:
                reply_text = str(reply.get("text") or "").strip()
                if reply_text:
                    dated.append((str(reply.get("created_at") or stamp), "assistant", reply_text[:800]))
        dated.sort(key=lambda row: row[0])
        turns = [{"role": role, "content": text} for _, role, text in dated]

    if current and turns and turns[-1]["role"] == "user" and turns[-1]["content"] == current:
        turns = turns[:-1]
    return turns[-max(1, limit) :]


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
        "auth_type": "rerequest",
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
            _meta_error_message(err, f"Graph GET {path} falló ({response.status_code})"),
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
            _meta_error_message(err, f"Graph DELETE {path} falló ({response.status_code})"),
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
            _meta_error_message(err, f"Graph POST {path} falló ({response.status_code})"),
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
            _meta_error_message(err, "No se pudo enviar el DM"),
            details=data,
        )
    return data


async def _upload_message_image(image_url: str) -> str:
    """Upload a reusable image to Meta; returns attachment_id."""
    conn = _require_connection()
    page_id = conn["page_id"]
    url = str(image_url or "").strip()
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{_graph_base()}/{page_id}/message_attachments",
            params={"access_token": conn["page_access_token"]},
            json={
                "message": {
                    "attachment": {
                        "type": "image",
                        "payload": {"url": url, "is_reusable": True},
                    }
                }
            },
        )
    data = response.json() if response.content else {}
    if response.status_code >= 400 or data.get("error"):
        err = data.get("error") or {}
        raise InstagramError(
            _meta_error_message(err, "No se pudo subir la imagen del mapa"),
            details=data,
        )
    attachment_id = str(data.get("attachment_id") or "").strip()
    if not attachment_id:
        raise InstagramError("Meta no devolvió attachment_id del mapa", details=data)
    return attachment_id


async def send_dm_image(recipient_id: str, image_url: str) -> dict[str, Any]:
    """Send a Google map image as the next bubble after the text."""
    conn = _require_connection()
    page_id = conn["page_id"]
    url = str(image_url or "").strip()
    if not url.startswith("https://"):
        raise InstagramError("Falta la URL pública de la imagen del mapa")

    # Prefer upload+attachment_id (more reliable than Meta re-fetching our URL).
    attachment_id = ""
    upload_error: dict[str, Any] | None = None
    try:
        attachment_id = await _upload_message_image(url)
    except InstagramError as exc:
        upload_error = exc.details if isinstance(exc.details, dict) else {"error": str(exc)}

    payload: dict[str, Any]
    if attachment_id:
        payload = {
            "recipient": {"id": recipient_id},
            "messaging_type": "RESPONSE",
            "message": {
                "attachment": {
                    "type": "image",
                    "payload": {"attachment_id": attachment_id},
                }
            },
        }
    else:
        payload = {
            "recipient": {"id": recipient_id},
            "messaging_type": "RESPONSE",
            "message": {
                "attachment": {
                    "type": "image",
                    "payload": {"url": url, "is_reusable": True},
                }
            },
        }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{_graph_base()}/{page_id}/messages",
            params={"access_token": conn["page_access_token"]},
            json=payload,
        )
    data = response.json() if response.content else {}
    if response.status_code >= 400 or data.get("error"):
        err = data.get("error") or {}
        if upload_error:
            data = {"send_error": data, "upload_error": upload_error}
        raise InstagramError(
            _meta_error_message(err, "No se pudo enviar la imagen del mapa"),
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


async def _hydrate_ig_message(msg: dict[str, Any], token: str) -> dict[str, Any]:
    """The conversations list often returns only ids. Load text and sender per message."""
    mid = str(msg.get("id") or "").strip()
    sender = msg.get("from") if isinstance(msg.get("from"), dict) else {}
    if not mid or (sender.get("id") and "message" in msg):
        return msg
    try:
        detail = await _graph_get(
            f"/{mid}",
            token,
            {"fields": "id,message,from,created_time"},
        )
    except InstagramError:
        return msg
    if not isinstance(detail, dict):
        return msg
    merged = dict(msg)
    for key, value in detail.items():
        if value is not None:
            merged[key] = value
    return merged


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
        messages: list[dict[str, Any]] = []
        for fields in ("id,message,from,created_time", "id,created_time"):
            try:
                messages = await _graph_pages(
                    f"/{thread_id}/messages",
                    token,
                    {"fields": fields, "limit": 50},
                    max_pages=20,
                )
                break
            except InstagramError:
                messages = []
        if not messages:
            messages = ((conv.get("messages") or {}).get("data")) or []
        peer_ids = [pid for pid in names if pid not in own_ids]
        peer_id = peer_ids[0] if peer_ids else ""
        peer_username = str(names.get(peer_id) or "").strip().lstrip("@")
        for msg in messages:
            msg = await _hydrate_ig_message(msg, token)
            sender = msg.get("from") if isinstance(msg.get("from"), dict) else {}
            sender_id = str(sender.get("id") or "")
            username = str(sender.get("username") or names.get(sender_id) or "").strip().lstrip("@")
            if not sender_id:
                continue
            own = sender_id in own_ids or (bool(username) and username.lower() == ig_username)
            external_id = f"message:{msg.get('id') or uuid.uuid4()}"
            guest_id = peer_id or ("" if own else sender_id)
            guest_name = peer_username or ("" if own else username)
            existing = find_by_external_id(external_id)
            if existing:
                changed = False
                if guest_id and str(existing.get("peer_id") or "") in own_ids.union({""}):
                    existing["peer_id"] = guest_id
                    changed = True
                if guest_name and not existing.get("peer_username"):
                    existing["peer_username"] = guest_name
                    changed = True
                if own and existing.get("direction") != "out":
                    existing["direction"] = "out"
                    changed = True
                if not own and username and not existing.get("author_username"):
                    existing["author_username"] = username
                    changed = True
                if changed:
                    upsert_inbox_item(existing)
                continue
            text = str(msg.get("message") or "").strip() or "[archivo]"
            item = upsert_inbox_item(
                {
                    "id": str(uuid.uuid4()),
                    "type": "message",
                    "thread_id": thread_id or guest_id or sender_id,
                    "external_id": external_id,
                    "meta_comment_id": None,
                    "media_id": None,
                    "author_username": None if own else (username or guest_name or None),
                    "author_id": sender_id,
                    "peer_id": guest_id,
                    "peer_username": guest_name or None,
                    "direction": "out" if own else "in",
                    "text": text,
                    "created_at": msg.get("created_time") or _now_iso(),
                    "status": "replied_human" if own else "pending",
                    "replies": [],
                    "escalation_reason": None,
                }
            )
            synced.append(item)
    return synced


def _strip_maps_urls(text: str) -> str:
    """Remove directions links from DM text. The map goes as an image bubble instead."""
    cleaned = re.sub(
        r"https?://(?:www\.)?(?:google\.com/maps|maps\.googleapis\.com|maps\.google\.com|goo\.gl/maps|maps\.app\.goo\.gl)\S*",
        "",
        text or "",
    )
    cleaned = re.sub(r"(?im)^\s*ver en google maps\s*$", "", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


async def send_inbox_reply(
    item_id: str,
    message: str,
    *,
    source: str = "human",
    map_image_url: str = "",
    maps_url: str = "",
) -> dict[str, Any]:
    item = get_inbox_item(item_id)
    if not item:
        raise InstagramError("Item de inbox no encontrado")
    text = _strip_maps_urls(message or "")
    if not text:
        raise InstagramError("Mensaje vacío")
    image = str(map_image_url or item.get("pending_map_image") or "").strip()
    open_url = str(maps_url or item.get("pending_maps_url") or "").strip()
    # Never put a long Google /maps/dir URL in the guest chat.
    if "google.com/maps/dir" in open_url or "maps.google.com" in open_url:
        open_url = ""
    ver_mapa = ver_mapa_message(open_url) if open_url.startswith("https://") else ""

    try:
        meta_result: dict[str, Any]
        if item.get("type") == "comment":
            comment_id = item.get("meta_comment_id")
            if not comment_id:
                raise InstagramError("Falta meta_comment_id")
            # Comments: short "Ver mapa" link only (no image attachment).
            comment_text = text if not ver_mapa else f"{text}\n\n{ver_mapa}"
            meta_result = await reply_to_comment(comment_id, comment_text)
            text = comment_text
        else:
            recipient_id = item.get("author_id") or item.get("peer_id")
            if not recipient_id:
                raise InstagramError("Falta author_id (IGSID) para DM")
            # 1) directions text  2) Google map image  3) "Ver mapa" short link
            meta_result = await send_dm(recipient_id, text)
            if image.startswith("https://"):
                try:
                    img = await send_dm_image(recipient_id, image)
                    replies = list(item.get("replies") or [])
                    replies.append(
                        {
                            "text": "",
                            "source": source,
                            "created_at": _now_iso(),
                            "meta_message_id": img.get("message_id") or img.get("id") or "",
                            "is_map": True,
                            "image_url": image,
                        }
                    )
                    item["replies"] = replies
                    item.pop("map_send_error", None)
                except InstagramError as map_exc:
                    item["map_send_error"] = str(map_exc)
                    item["pending_map_image"] = image
            if ver_mapa:
                try:
                    link_meta = await send_dm(recipient_id, ver_mapa)
                    replies = list(item.get("replies") or [])
                    replies.append(
                        {
                            "text": ver_mapa,
                            "source": source,
                            "created_at": _now_iso(),
                            "meta_message_id": link_meta.get("message_id") or link_meta.get("id") or "",
                            "is_map_link": True,
                        }
                    )
                    item["replies"] = replies
                except InstagramError:
                    pass
    except InstagramError as exc:
        if item.get("type") == "message" and _is_window_closed_error(exc):
            item["status"] = "window_closed"
            item["escalation_reason"] = WINDOW_CLOSED_MESSAGE
            item["updated_at"] = _now_iso()
            upsert_inbox_item(item)
            raise InstagramError(WINDOW_CLOSED_MESSAGE, details=exc.details) from exc
        raise

    reply = {
        "text": text,
        "source": source,
        "created_at": _now_iso(),
        "meta_message_id": meta_result.get("id") or meta_result.get("message_id"),
    }
    replies = list(item.get("replies") or [])
    # Avoid storing the same outbound bubble twice (double-click / echo race).
    last = replies[-1] if replies else None
    if last and str(last.get("text") or "").strip() == text:
        item["status"] = "replied_deepseek" if source == "deepseek" else "replied_human"
        item["updated_at"] = _now_iso()
        item.pop("pending_map_image", None)
        item.pop("pending_maps_url", None)
        return upsert_inbox_item(item)
    # Order local history: text → map image → Ver mapa link.
    extras: list[dict[str, Any]] = []
    while replies and (replies[-1].get("is_map") or replies[-1].get("is_map_link")):
        extras.insert(0, replies.pop())
    replies.append(reply)
    replies.extend(extras)
    item["replies"] = replies
    item["status"] = "replied_deepseek" if source == "deepseek" else "replied_human"
    item["updated_at"] = _now_iso()
    item.pop("pending_map_image", None)
    item.pop("pending_maps_url", None)
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
            inbound = item.get("text") or ""
            decision = await generate_instagram_reply(
                db=db,
                inbound_text=inbound,
                item_type=item.get("type") or "message",
                author_username=item.get("author_username"),
                person_history=author_channel_history(
                    author_username=item.get("author_username"),
                    author_id=item.get("author_id"),
                    exclude_id=item.get("id"),
                ),
                thread_history=thread_history_for_reply(item, inbound_text=inbound),
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

    return await send_inbox_reply(
        item_id,
        decision["message"],
        source="deepseek",
        map_image_url=str(decision.get("map_image_url") or ""),
        maps_url=str(decision.get("maps_url") or ""),
    )


async def suggest_reply(item_id: str) -> dict[str, Any]:
    item = get_inbox_item(item_id)
    if not item:
        raise InstagramError("Item de inbox no encontrado")
    db = SessionLocal()
    try:
        inbound = item.get("text") or ""
        decision = await generate_instagram_reply(
            db=db,
            inbound_text=inbound,
            item_type=item.get("type") or "message",
            author_username=item.get("author_username"),
            person_history=author_channel_history(
                author_username=item.get("author_username"),
                author_id=item.get("author_id"),
                exclude_id=item.get("id"),
            ),
            thread_history=thread_history_for_reply(item, inbound_text=inbound),
        )
    finally:
        db.close()
    draft = _strip_maps_urls(decision.get("message") or "")
    decision["message"] = draft
    item["deepseek_draft"] = draft
    item["deepseek_action"] = decision.get("action")
    item["escalation_reason"] = decision.get("reason")
    map_image = str(decision.get("map_image_url") or "").strip()
    maps_url = str(decision.get("maps_url") or "").strip()
    if map_image.startswith("https://"):
        item["pending_map_image"] = map_image
    else:
        item.pop("pending_map_image", None)
    if maps_url.startswith("https://") and "google.com/maps/dir" not in maps_url:
        item["pending_maps_url"] = maps_url
    else:
        item.pop("pending_maps_url", None)
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
    peer_id: str | None = None,
    direction: str = "in",
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
            "peer_id": peer_id or author_id,
            "direction": direction if direction in {"in", "out"} else "in",
            "text": text or "",
            "created_at": _now_iso(),
            "status": "replied_human" if direction == "out" else "pending",
            "replies": [],
            "escalation_reason": None,
        }
    )


def _related_dm_items(item: dict[str, Any]) -> list[dict[str, Any]]:
    """All stored bubbles of the same Instagram DM, including older messages and our replies."""
    messages = [row for row in list_inbox() if row.get("type") == "message"]
    item_id = item.get("id")
    for group in _group_dm_items(messages):
        if any(row.get("id") == item_id for row in group):
            group.sort(key=lambda row: str(row.get("created_at") or ""))
            return group
    return [item]


def conversation_turns(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Chronological bubbles for the admin chat."""
    if item.get("type") != "message":
        turns = [
            {
                "role": "in",
                "text": item.get("text") or "",
                "created_at": item.get("created_at"),
                "author": item.get("author_username") or item.get("author_id") or "",
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
                    "item_id": item.get("id"),
                    "reply_index": index,
                    "is_map": bool(reply.get("is_map")),
                    "image_url": reply.get("image_url") or "",
                }
            )
        return turns

    related = _related_dm_items(item)
    seen_out: set[str] = set()
    # Collect every stored outbound reply first so Meta echo bubbles do not duplicate them.
    for row in related:
        for reply in row.get("replies") or []:
            mid = str(reply.get("meta_message_id") or "").strip()
            if mid:
                seen_out.add(f"message:{mid}")
                seen_out.add(f"message:message:{mid}")
            text = str(reply.get("text") or "").strip()
            if text:
                seen_out.add(f"text:{text}")
                seen_out.add(f"text:{_normalize_bubble(text)}")

    turns: list[dict[str, Any]] = []
    for row in related:
        direction = "out" if row.get("direction") == "out" else "in"
        external_id = str(row.get("external_id") or "")
        text = str(row.get("text") or "").strip()
        if direction == "out" and (
            external_id in seen_out
            or f"message:{external_id}" in seen_out
            or f"text:{text}" in seen_out
            or f"text:{_normalize_bubble(text)}" in seen_out
        ):
            continue
        turns.append(
            {
                "role": direction,
                "text": row.get("text") or "",
                "created_at": row.get("created_at"),
                "author": row.get("author_username") or ("Tú" if direction == "out" else row.get("author_id") or ""),
                "source": "human" if direction == "out" else "",
                "item_id": row.get("id"),
                "reply_index": None,
            }
        )
        if direction == "in":
            for index, reply in enumerate(row.get("replies") or []):
                reply_text = str(reply.get("text") or "").strip()
                turns.append(
                    {
                        "role": "out",
                        "text": reply.get("text") or "",
                        "created_at": reply.get("created_at"),
                        "source": reply.get("source") or "human",
                        "author": "DeepSeek" if reply.get("source") == "deepseek" else "Tú",
                        "item_id": row.get("id"),
                        "reply_index": index,
                        "is_map": bool(reply.get("is_map")),
                        "image_url": reply.get("image_url") or "",
                    }
                )
                if reply_text:
                    seen_out.add(f"text:{reply_text}")
                    seen_out.add(f"text:{_normalize_bubble(reply_text)}")
    turns.sort(key=lambda turn: str(turn.get("created_at") or ""))
    return _dedupe_turns(turns)


def _normalize_bubble(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def _dedupe_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop consecutive outbound bubbles with the same text (reply + Meta echo)."""
    cleaned: list[dict[str, Any]] = []
    seen_out_text: set[str] = set()
    for turn in turns:
        if turn.get("role") == "out":
            key = _normalize_bubble(str(turn.get("text") or ""))
            if key and key in seen_out_text:
                continue
            if key:
                seen_out_text.add(key)
        cleaned.append(turn)
    return cleaned


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


def _echo_event(event: dict[str, Any]) -> tuple[str, str, str]:
    """Return (guest_id, mid, text) when the hostel replies from the Instagram app."""
    message = event.get("message")
    if not isinstance(message, dict) or not message.get("is_echo") or message.get("is_deleted"):
        return "", "", ""
    recipient = str((event.get("recipient") or {}).get("id") or "")
    if not recipient or recipient in _own_instagram_ids():
        return "", "", ""
    text = str(message.get("text") or "").strip()
    if not text and message.get("attachments"):
        text = "[archivo]"
    return recipient, str(message.get("mid") or ""), text


async def _store_dm_event(event: dict[str, Any]) -> dict[str, Any] | None:
    guest_id, echo_mid, echo_text = _echo_event(event)
    if guest_id and echo_text:
        return ingest_inbound(
            item_type="message",
            external_id=f"message:{echo_mid or f'{guest_id}-{int(time.time())}'}",
            text=echo_text,
            author_id=next(iter(_own_instagram_ids()), guest_id),
            thread_id=guest_id,
            peer_id=guest_id,
            direction="out",
        )
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
        peer_id=sender,
        direction="in",
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
