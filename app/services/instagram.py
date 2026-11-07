"""Instagram Graph API: OAuth, inbox store, comments/DMs, auto-reply pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
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

CONNECTION_FILE = settings.uploads_dir / "cache" / "instagram_connection.json"
INBOX_FILE = settings.uploads_dir / "cache" / "instagram_inbox.json"
AUTO_REPLY_FILE = settings.uploads_dir / "cache" / "deepseek_auto_reply.json"
MEDIA_COMMENT_COUNTS_FILE = settings.uploads_dir / "cache" / "instagram_media_comment_counts.json"
MENTION_WATCH_FILE = settings.uploads_dir / "cache" / "instagram_mention_watch.json"
_inbox_lock = threading.Lock()
_inbox_cache: dict[str, Any] | None = None
# Guests type in bursts ("hola" / "si" / "estoy interesado"). Wait after the last
# bubble, then answer once with everything that arrived together.
AUTO_REPLY_DEBOUNCE_SECONDS = 7.0
# Even if they keep sending bubbles, force one reply before this cap.
AUTO_REPLY_MAX_BURST_SECONDS = 20.0
# Don't ignore a new "hola" forever after an old greeting reply.
GREETING_SKIP_WINDOW_SECONDS = 120.0
_auto_reply_debounce_tasks: dict[str, asyncio.Task[Any]] = {}
_auto_reply_debounce_deadline: dict[str, float] = {}
_auto_reply_debounce_lock = asyncio.Lock()
_auto_reply_peer_locks: dict[str, asyncio.Lock] = {}
logger = logging.getLogger(__name__)
_HELP_OFFER_MARKERS = (
    "en qué te puedo ayudar",
    "en que te puedo ayudar",
    "how can i help",
    "em que posso",
)

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

# DMs + public comments/mentions. Mentions = @hotel in captions/comments on
# OTHER people's posts/reels (Stories are not supported by Meta).
# Page subscribed_apps only accepts Page messaging fields — NOT Instagram
# comments/mentions (those are configured in App Dashboard → Instagram webhooks).
MESSAGE_WEBHOOK_FIELDS = (
    "messages",
    "messaging_postbacks",
    "message_reads",
    "messaging_referrals",
    "message_reactions",
    "message_echoes",
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
    sample = raw.lower()
    # Do NOT treat bare Graph codes 10/100 as window-closed — those are also
    # used for invalid attachments and would hide real map-send failures.
    if (
        "fuera del período" in sample
        or "outside of the allowed window" in sample
        or "outside the allowed window" in sample
        or ("(#10)" in sample and "window" in sample)
        or "human agent tagging is being deprecated" in sample
    ):
        return WINDOW_CLOSED_MESSAGE
    code = err.get("code")
    sub = err.get("error_subcode")
    if code == 10 and sub in {2018278, 2534022, 1545041}:
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
        "media_id": item.get("media_id"),
        "author_username": item.get("author_username"),
        "author_id": item.get("author_id"),
        "peer_id": item.get("peer_id"),
        "peer_username": peer_username if peer_username is not None else item.get("peer_username"),
        "direction": item.get("direction"),
        "text": text,
        "created_at": item.get("created_at"),
        "status": item.get("status"),
        "escalation_reason": item.get("escalation_reason"),
        "message_count": message_count,
    }


def _comment_group_key(item: dict[str, Any]) -> str:
    media = str(item.get("media_id") or item.get("thread_id") or "")
    author = str(item.get("author_id") or "").strip()
    if not author:
        author = str(item.get("author_username") or "").strip().lower().lstrip("@")
    return f"{media}|{author}"


def _group_comment_items(comments: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """One bucket per guest on the same Instagram post (comment chain)."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in comments:
        buckets.setdefault(_comment_group_key(item), []).append(item)
    return list(buckets.values())


def _pick_group_latest(
    group: list[dict[str, Any]],
    *,
    status: str | None,
) -> dict[str, Any] | None:
    if not group:
        return None
    if status == "pending":
        pending = [row for row in group if row.get("status") == "pending"]
        if not pending:
            return None
        return max(pending, key=lambda row: str(row.get("created_at") or ""))
    latest = max(group, key=lambda row: str(row.get("created_at") or ""))
    if status and latest.get("status") != status:
        # Keep the conversation visible when filtering by a status that appears
        # anywhere in the chain (e.g. skipped nested replies).
        if not any(row.get("status") == status for row in group):
            return None
        matching = [row for row in group if row.get("status") == status]
        return max(matching, key=lambda row: str(row.get("created_at") or ""))
    return latest


def list_inbox_page(
    *,
    item_type: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Slim rows: one per DM peer, one per guest comment chain on a post."""
    # Group ALL items first, then apply status filter on the conversation.
    # Filtering before grouping made each bubble look like a new chat.
    items = list_inbox(item_type=item_type)
    comments = [item for item in items if item.get("type") != "message"]
    messages = [item for item in items if item.get("type") == "message"]
    own_ids = _own_instagram_ids()
    ig_username = str((load_connection() or {}).get("ig_username") or "").strip().lower()
    rows: list[dict[str, Any]] = []
    for group in _group_dm_items(messages):
        latest = _pick_group_latest(group, status=status)
        if not latest:
            continue
        guest = _guest_id(latest, own_ids) or _guest_id(group[0], own_ids)
        row = _page_row(
            latest,
            message_count=len(group),
            peer_username=_client_username(group, own_ids, ig_username) or None,
        )
        if guest:
            row["peer_id"] = guest
        rows.append(row)
    for group in _group_comment_items(comments):
        latest = _pick_group_latest(group, status=status)
        if not latest:
            continue
        row = _page_row(latest, message_count=len(group))
        row["author_username"] = latest.get("author_username")
        rows.append(row)
    rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return rows


def get_inbox_item(item_id: str) -> dict[str, Any] | None:
    for item in _load_inbox_raw().get("items") or []:
        if item.get("id") == item_id:
            return item
    return None


def get_dm_thread_by_peer(peer_id: str) -> dict[str, Any] | None:
    """Return the newest inbox bubble for a guest so the admin opens one stable chat."""
    guest = str(peer_id or "").strip()
    if not guest:
        return None
    own_ids = _own_instagram_ids()
    matches: list[dict[str, Any]] = []
    for item in list_inbox(item_type="message"):
        if _guest_id(item, own_ids) == guest:
            matches.append(item)
    if not matches:
        return None
    return max(matches, key=lambda row: str(row.get("created_at") or ""))


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
        # Bump when poller behavior changes — used to verify prod deploy.
        "comment_poller": "reels-collab-v14",
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
    """Ask Meta to deliver DMs, comments, and @mentions for this Page/IG account."""
    conn = conn or _require_connection()
    page_id = conn.get("page_id")
    ig_user_id = conn.get("ig_user_id")
    token = conn.get("page_access_token")
    if not page_id or not token:
        return {"ok": False, "error": "Falta la página de Facebook conectada."}
    fields = ",".join(MESSAGE_WEBHOOK_FIELDS)
    try:
        page_result = await _graph_post(
            f"/{page_id}/subscribed_apps",
            token,
            {"subscribed_fields": fields},
        )
        ig_result: dict[str, Any] | None = None
        # Instagram object subscription (mentions/comments on others' media).
        if ig_user_id:
            try:
                ig_result = await _graph_post(
                    f"/{ig_user_id}/subscribed_apps",
                    token,
                    {"subscribed_fields": fields},
                )
            except InstagramError as exc:
                logger.warning("IG subscribed_apps partial failure: %s", exc)
                ig_result = {"ok": False, "error": str(exc)}
        return {"ok": True, "result": page_result, "ig_result": ig_result}
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


async def reply_to_mention(
    *,
    media_id: str,
    message: str,
    comment_id: str | None = None,
) -> dict[str, Any]:
    """Public reply when someone @mentions the hotel on another account's post/reel."""
    conn = _require_connection()
    ig_user_id = conn.get("ig_user_id")
    if not ig_user_id:
        raise InstagramError("Falta ig_user_id para responder menciones")
    if not media_id:
        raise InstagramError("Falta media_id para responder la mención")
    payload: dict[str, Any] = {"media_id": media_id, "message": message}
    if comment_id:
        payload["comment_id"] = comment_id
    return await _graph_post(
        f"/{ig_user_id}/mentions",
        conn["page_access_token"],
        payload,
    )


async def fetch_mention_details(
    *,
    media_id: str | None,
    comment_id: str | None,
) -> dict[str, Any]:
    """Webhook mentions only send ids — pull caption/comment text + author from Graph."""
    conn = _require_connection()
    ig_user_id = conn["ig_user_id"]
    token = conn["page_access_token"]
    text = ""
    author_id = ""
    author_username = ""
    if comment_id:
        try:
            data = await _graph_get(
                f"/{ig_user_id}",
                token,
                {
                    "fields": (
                        f"mentioned_comment.comment_id({comment_id})"
                        "{text,username,from{id,username},media{id}}"
                    ),
                },
            )
            mentioned = data.get("mentioned_comment") or {}
            text = str(mentioned.get("text") or "")
            author_username = str(
                mentioned.get("username")
                or (mentioned.get("from") or {}).get("username")
                or ""
            )
            author_id = str((mentioned.get("from") or {}).get("id") or "")
            media_id = media_id or str((mentioned.get("media") or {}).get("id") or "")
        except InstagramError as exc:
            logger.warning("mentioned_comment fetch failed (%s): %s", comment_id, exc)
    if not text and media_id:
        try:
            data = await _graph_get(
                f"/{ig_user_id}",
                token,
                {
                    "fields": (
                        f"mentioned_media.media_id({media_id})"
                        "{caption,username,owner{id,username}}"
                    ),
                },
            )
            mentioned = data.get("mentioned_media") or {}
            text = str(mentioned.get("caption") or "")
            if not author_username:
                author_username = str(
                    mentioned.get("username")
                    or (mentioned.get("owner") or {}).get("username")
                    or ""
                )
            if not author_id:
                author_id = str((mentioned.get("owner") or {}).get("id") or "")
        except InstagramError as exc:
            logger.warning("mentioned_media fetch failed (%s): %s", media_id, exc)
    return {
        "text": text,
        "author_id": author_id,
        "author_username": author_username,
        "media_id": media_id or "",
        "comment_id": comment_id or "",
    }


async def send_dm_whatsapp_button(
    recipient_id: str,
    text: str,
    whatsapp_url: str,
) -> dict[str, Any]:
    """Send text + WhatsApp URL as a button (no OG preview image of the WhatsApp account)."""
    conn = _require_connection()
    page_id = conn["page_id"]
    body = (text or "").strip() or "Escríbenos por WhatsApp."
    if len(body) > 640:
        body = body[:637].rstrip() + "…"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": body,
                    "buttons": [
                        {
                            "type": "web_url",
                            "url": whatsapp_url,
                            "title": "WhatsApp",
                        }
                    ],
                },
            }
        },
    }
    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(
            f"{_graph_base()}/{page_id}/messages",
            params={"access_token": conn["page_access_token"]},
            json=payload,
        )
    data = response.json() if response.content else {}
    if response.status_code >= 400 or data.get("error"):
        err = data.get("error") or {}
        raise InstagramError(
            _meta_error_message(err, "No se pudo enviar el botón de WhatsApp"),
            details=data,
        )
    data["_whatsapp_button"] = True
    return data


async def send_dm(recipient_id: str, message: str) -> dict[str, Any]:
    from app.services.deepseek_instagram import extract_whatsapp_link, whatsapp_click_url

    text, wa_direct = extract_whatsapp_link(message or "")
    if wa_direct:
        # Prefer button (no OG card). Never fall back to raw wa.me in text.
        try:
            return await send_dm_whatsapp_button(
                recipient_id,
                text or "Escríbenos por WhatsApp.",
                wa_direct,
            )
        except InstagramError as button_exc:
            logger.warning(
                "WhatsApp button failed, using preview-safe share link: %s",
                button_exc,
            )
            share = whatsapp_click_url(wa_direct)
            text = f"{text or 'Escríbenos por WhatsApp.'} {share}".strip()
    else:
        text = (message or "").strip()
    if not text:
        raise InstagramError("Mensaje vacío")
    conn = _require_connection()
    page_id = conn["page_id"]
    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(
            f"{_graph_base()}/{page_id}/messages",
            params={"access_token": conn["page_access_token"]},
            json={
                "recipient": {"id": recipient_id},
                "message": {"text": text},
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


def _local_map_bytes(image_url: str) -> bytes:
    """Read a hosted /uploads/maps image from disk when possible."""
    url = str(image_url or "").strip()
    marker = "/uploads/maps/"
    if marker not in url:
        return b""
    name = url.split(marker, 1)[1].split("?", 1)[0].strip("/")
    if not name or "/" in name or "\\" in name:
        return b""
    path = settings.uploads_dir / "maps" / name
    try:
        data = path.read_bytes()
    except OSError:
        return b""
    if data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG"):
        return data
    return b""


async def _download_image_bytes(image_url: str) -> bytes:
    local = _local_map_bytes(image_url)
    if local:
        return local
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        response = await client.get(image_url)
        response.raise_for_status()
        content = response.content
    if not (content.startswith(b"\xff\xd8\xff") or content.startswith(b"\x89PNG")):
        raise InstagramError("La imagen del mapa no es un JPEG/PNG válido")
    return content


async def _upload_message_image(image_url: str) -> str:
    """Upload map bytes to Meta (multipart). Avoids Meta fetching our public URL."""
    conn = _require_connection()
    page_id = conn["page_id"]
    url = str(image_url or "").strip()
    try:
        content = await _download_image_bytes(url)
    except (httpx.HTTPError, InstagramError) as exc:
        raise InstagramError(f"No se pudo leer la imagen del mapa: {exc}") from exc

    filename = "mapa.jpg" if content.startswith(b"\xff\xd8\xff") else "mapa.png"
    mime = "image/jpeg" if filename.endswith(".jpg") else "image/png"
    message = json.dumps(
        {"attachment": {"type": "image", "payload": {"is_reusable": True}}}
    )
    files = {"filedata": (filename, content, mime)}
    data = {"message": message}
    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(
            f"{_graph_base()}/{page_id}/message_attachments",
            params={"access_token": conn["page_access_token"]},
            data=data,
            files=files,
        )
    payload = response.json() if response.content else {}
    if response.status_code >= 400 or payload.get("error"):
        # Fallback: let Meta fetch the public HTTPS URL.
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
        payload = response.json() if response.content else {}
        if response.status_code >= 400 or payload.get("error"):
            err = payload.get("error") or {}
            raise InstagramError(
                _meta_error_message(err, "No se pudo subir la imagen del mapa a Meta"),
                details=payload,
            )
    attachment_id = str(payload.get("attachment_id") or "").strip()
    if not attachment_id:
        raise InstagramError("Meta no devolvió attachment_id del mapa", details=payload)
    return attachment_id


async def test_map_image_upload(image_url: str = "") -> dict[str, Any]:
    """Admin diagnostic: generate/upload a map image to Meta without messaging a guest."""
    url = str(image_url or "").strip()
    if not url.startswith("https://"):
        from app.services.hostel_directions import directions_from_hostel

        route = directions_from_hostel("Santander Arena")
        url = str(route.get("map_image_url") or "").strip()
        if not url:
            return {"ok": False, "error": "No se pudo generar map_image_url", "route_ok": route.get("ok")}
    try:
        attachment_id = await _upload_message_image(url)
    except InstagramError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "details": exc.details,
            "map_image_url": url,
        }
    return {"ok": True, "attachment_id": attachment_id, "map_image_url": url}


async def send_dm_map_card(
    recipient_id: str,
    *,
    image_url: str,
    open_url: str,
    title: str = "Google Maps",
    subtitle: str = "Toca para abrir la ruta",
) -> dict[str, Any]:
    """Send Google Static Map as a tappable card that opens Google Maps (no URL in text)."""
    conn = _require_connection()
    page_id = conn["page_id"]
    image = str(image_url or "").strip()
    link = str(open_url or "").strip()
    if not image.startswith("https://"):
        raise InstagramError("Falta la URL pública de la imagen del mapa")
    if not link.startswith("https://"):
        raise InstagramError("Falta el enlace de Google Maps para abrir al tocar")

    element: dict[str, Any] = {
        "title": (title or "Google Maps")[:80],
        "subtitle": (subtitle or "Toca para abrir la ruta")[:80],
        "image_url": image,
        "default_action": {
            "type": "web_url",
            "url": link,
        },
        "buttons": [
            {
                "type": "web_url",
                "url": link,
                "title": "Abrir mapa",
            }
        ],
    }
    attempts: list[dict[str, Any]] = [
        # Preferred: generic template — tap image/card opens Google Maps.
        {
            "recipient": {"id": recipient_id},
            "message": {
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": "generic",
                        "elements": [element],
                    },
                }
            },
        },
        # Fallback: plain image (not tappable to Maps).
        {
            "recipient": {"id": recipient_id},
            "message": {
                "attachment": {
                    "type": "image",
                    "payload": {"url": image, "is_reusable": True},
                }
            },
        },
    ]
    last_error: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=60.0) as client:
        for payload in attempts:
            response = await client.post(
                f"{_graph_base()}/{page_id}/messages",
                params={"access_token": conn["page_access_token"]},
                json=payload,
            )
            data = response.json() if response.content else {}
            if response.status_code < 400 and not data.get("error"):
                data["_map_card"] = payload["message"]["attachment"]["type"]
                return data
            last_error = data
    err = (last_error.get("error") or {}) if isinstance(last_error, dict) else {}
    raise InstagramError(
        _meta_error_message(err, "No se pudo enviar el mapa de Google"),
        details=last_error,
    )


async def send_dm_image(recipient_id: str, image_url: str, *, open_url: str = "") -> dict[str, Any]:
    """Send Google map: tappable card when open_url is set, else plain image."""
    link = str(open_url or "").strip()
    if link.startswith("https://"):
        return await send_dm_map_card(recipient_id, image_url=image_url, open_url=link)
    # Plain image fallback (no open link available).
    conn = _require_connection()
    page_id = conn["page_id"]
    url = str(image_url or "").strip()
    if not url.startswith("https://"):
        raise InstagramError("Falta la URL pública de la imagen del mapa")
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{_graph_base()}/{page_id}/messages",
            params={"access_token": conn["page_access_token"]},
            json={
                "recipient": {"id": recipient_id},
                "message": {
                    "attachment": {
                        "type": "image",
                        "payload": {"url": url, "is_reusable": True},
                    }
                },
            },
        )
    data = response.json() if response.content else {}
    if response.status_code >= 400 or data.get("error"):
        err = data.get("error") or {}
        raise InstagramError(
            _meta_error_message(err, "No se pudo enviar la imagen del mapa"),
            details=data,
        )
    return data


# Meta: comment webhooks do NOT fire for Reels. Poll Graph instead.
COMMENT_POLL_INTERVAL_SECONDS = 45.0
COMMENT_POLL_MEDIA_LIMIT = 150
COMMENT_SYNC_MEDIA_LIMIT = 500
# Only ingest / auto-answer comments from the last 2 hours.
COMMENT_MAX_AGE_SECONDS = 2 * 3600
# Always open /comments for the newest N media; otherwise only when count grew.
COMMENT_ALWAYS_CHECK_NEWEST = 25
COMMENT_FETCH_BUDGET = 80
COMMENT_FETCH_BUDGET_SYNC = 220


def _inbound_is_fresh(
    stamp: str | None,
    *,
    max_age_seconds: float = COMMENT_MAX_AGE_SECONDS,
) -> bool:
    """True only for guest messages younger than max_age (default 2h)."""
    parsed = _parse_iso(stamp)
    if not parsed:
        # If Meta sends a weird stamp, keep it — better reply than silently drop.
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - parsed).total_seconds()
    return 0 <= age <= max_age_seconds


def _is_reel_media(media: dict[str, Any]) -> bool:
    product = str(media.get("media_product_type") or "").upper()
    permalink = str(media.get("permalink") or "").lower()
    return product == "REELS" or "/reel/" in permalink


def _load_media_comment_counts() -> dict[str, int]:
    try:
        if not MEDIA_COMMENT_COUNTS_FILE.exists():
            return {}
        raw = json.loads(MEDIA_COMMENT_COUNTS_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return {str(k): int(v or 0) for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        return {}


def _save_media_comment_counts(counts: dict[str, int]) -> None:
    try:
        MEDIA_COMMENT_COUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Keep file small: most recent media ids only.
        items = list(counts.items())[-400:]
        MEDIA_COMMENT_COUNTS_FILE.write_text(
            json.dumps(dict(items), ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist Instagram media comment counts")


def _load_mention_watch() -> list[str]:
    try:
        if not MENTION_WATCH_FILE.exists():
            return []
        raw = json.loads(MENTION_WATCH_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        return [str(x) for x in raw if str(x).strip()]
    except Exception:  # noqa: BLE001
        return []


def watch_mention_media(media_id: str | None) -> None:
    """Keep watching others' posts/reels where @blackcathostal was mentioned."""
    mid = str(media_id or "").strip()
    if not mid:
        return
    items = _load_mention_watch()
    if mid in items:
        return
    items.append(mid)
    try:
        MENTION_WATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        MENTION_WATCH_FILE.write_text(
            json.dumps(items[-80:], ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist Instagram mention watch list")


_BOT_PUBLIC_PHRASE_RE = re.compile(
    r"("
    r"en qu[eé] te podemos ayudar|"
    r"how can we help|"
    r"em que podemos ajudar|"
    r"comment pouvons-nous vous aider|"
    r"wie k[oö]nnen wir helfen|"
    r"escr[ií]benos por interno|"
    r"escribe(?:nos)?\s+por\s+interno|"
    r"write us (?:in )?(?:dm|private|interno)|"
    r"manda(?:r)?\s+(?:um\s+)?direct"
    r")",
    re.IGNORECASE,
)

_FOREIGN_REEL_DM_REDIRECT = {
    "es": "Hola! Escríbenos por interno para ayudarte con eso.",
    "en": "Hi! Please DM us and we'll help you with that.",
    "pt": "Oi! Nos chama no direct que te ajudamos.",
}


def _looks_like_own_public_reply(text: str) -> bool:
    """Detect our own Mentions-API replies so the poller does not answer itself."""
    sample = (text or "").strip()
    if not sample:
        return False
    return bool(_BOT_PUBLIC_PHRASE_RE.search(sample))


def _ingest_comment_row(
    c: dict[str, Any],
    *,
    media: dict[str, Any],
    item_type: str = "comment",
    collaborative: bool = False,
) -> dict[str, Any] | None:
    media_id = str(media.get("id") or "").strip()
    comment_id = c.get("id")
    if not media_id or not comment_id:
        return None
    kind = "mention" if item_type == "mention" else "comment"
    external_id = f"{kind}:{comment_id}"
    if find_by_external_id(external_id):
        return None
    # Also skip if already stored under the other prefix.
    if find_by_external_id(f"comment:{comment_id}") or find_by_external_id(
        f"mention:{comment_id}"
    ):
        return None
    from_user = c.get("from") or {}
    author_username = c.get("username") or from_user.get("username")
    author_id = from_user.get("id")
    text = str(c.get("text") or "")
    if _is_own_instagram_actor(
        author_id=str(author_id or ""),
        author_username=str(author_username or ""),
    ):
        return None
    # Own Mentions-API replies often arrive without username — still skip them.
    if _looks_like_own_public_reply(text):
        return None
    if kind == "mention" and not str(author_id or "").strip() and not str(
        author_username or ""
    ).strip():
        return None
    created = c.get("timestamp") or _now_iso()
    # Drop anything older than 2h — do not pollute the inbox with ancient threads.
    if not _inbound_is_fresh(str(created)):
        return None
    return upsert_inbox_item(
        {
            "id": str(uuid.uuid4()),
            "type": kind,
            "thread_id": media_id,
            "external_id": external_id,
            "meta_comment_id": comment_id,
            "media_id": media_id,
            "author_username": author_username,
            "author_id": author_id,
            "text": text,
            "created_at": created,
            "status": "pending",
            "replies": [],
            "escalation_reason": None,
            "permalink": media.get("permalink"),
            "media_type": media.get("media_type") or media.get("media_product_type"),
            "foreign_media": kind == "mention",
            "collaborative": bool(collaborative),
        }
    )


async def _fetch_media_comments(media_id: str, token: str) -> list[dict[str, Any]]:
    try:
        data = await _graph_get(
            f"/{media_id}/comments",
            token,
            {"fields": "id,text,username,timestamp,from", "limit": 30},
        )
    except InstagramError as exc:
        logger.warning("comments fetch failed for media %s: %s", media_id, exc)
        return []
    return list(data.get("data") or [])


async def _fetch_mentioned_media_comments(
    *,
    ig_user_id: str,
    media_id: str,
    token: str,
) -> list[dict[str, Any]]:
    """Foreign media often blocks GET /{media-id}/comments — use mentioned_media."""
    mid = str(media_id or "").strip()
    if not mid:
        return []
    try:
        data = await _graph_get(
            f"/{ig_user_id}",
            token,
            {
                "fields": (
                    f"mentioned_media.media_id({mid})"
                    "{id,caption,comments.limit(30){id,text,username,timestamp,from}}"
                ),
            },
        )
    except InstagramError as exc:
        logger.warning("mentioned_media comments failed for %s: %s", mid, exc)
        return []
    mentioned = data.get("mentioned_media") or {}
    return list((mentioned.get("comments") or {}).get("data") or [])


async def sync_recent_comments(
    limit_media: int = COMMENT_POLL_MEDIA_LIMIT,
    *,
    auto_reply: bool = False,
    fetch_budget: int | None = None,
    rotate_offset: int = 0,
) -> list[dict[str, Any]]:
    """Fast comment poll: list media counts, fetch /comments only when needed.

    Detects new comments on OLD reels by comparing comments_count to a local cache
    (Meta does not webhook Reel comments).
    """
    conn = _require_connection()
    ig_user_id = conn["ig_user_id"]
    token = conn["page_access_token"]
    wanted = max(10, min(int(limit_media or COMMENT_POLL_MEDIA_LIMIT), COMMENT_SYNC_MEDIA_LIMIT))
    per_page = 50
    pages = max(1, (wanted + per_page - 1) // per_page)
    budget = max(
        10,
        min(
            int(fetch_budget or COMMENT_FETCH_BUDGET),
            COMMENT_FETCH_BUDGET_SYNC,
        ),
    )
    # Lightweight list — no nested comments (those hang / come empty on Reels).
    media_rows = await _graph_pages(
        f"/{ig_user_id}/media",
        token,
        {
            "fields": "id,caption,timestamp,permalink,media_type,media_product_type,comments_count",
            "limit": per_page,
        },
        max_pages=min(pages, 12),
    )
    prev_counts = _load_media_comment_counts()
    next_counts = dict(prev_counts)
    priority: list[dict[str, Any]] = []
    with_comments: list[dict[str, Any]] = []
    for index, media in enumerate(media_rows):
        media_id = str(media.get("id") or "").strip()
        if not media_id:
            continue
        count = int(media.get("comments_count") or 0)
        previous = int(prev_counts.get(media_id, -1))
        # IMPORTANT: do NOT write the live count into cache until we actually
        # fetch comments. Otherwise a new comment is "acked" and never read.
        if count <= 0:
            next_counts[media_id] = 0
            continue
        with_comments.append(media)
        if index < COMMENT_ALWAYS_CHECK_NEWEST or (previous >= 0 and count > previous):
            priority.append(media)
        elif previous < 0 and _is_reel_media(media):
            priority.append(media)

    # Rotate through the rest so older quiet posts are not permanently starved.
    if with_comments and rotate_offset:
        rot = abs(int(rotate_offset)) % len(with_comments)
        rotated = with_comments[rot:] + with_comments[:rot]
    else:
        rotated = with_comments

    seen_ids: set[str] = set()
    to_fetch: list[dict[str, Any]] = []
    for media in [*priority, *rotated]:
        mid = str(media.get("id") or "")
        if not mid or mid in seen_ids:
            continue
        seen_ids.add(mid)
        to_fetch.append(media)
        if len(to_fetch) >= budget:
            break

    synced: list[dict[str, Any]] = []
    to_reply: list[str] = []
    scanned_fresh = 0
    raw_comments = 0
    fresh_samples: list[str] = []
    raw_samples: list[str] = []

    async def _load_one(media: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        mid = str(media.get("id") or "")
        rows = await _fetch_media_comments(mid, token)
        return media, rows

    # Parallel Graph reads — sequential was too slow and covered too little.
    chunk_size = 12
    loaded: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for start in range(0, len(to_fetch), chunk_size):
        chunk = to_fetch[start : start + chunk_size]
        loaded.extend(await asyncio.gather(*[_load_one(media) for media in chunk]))

    for media, rows in loaded:
        media_id = str(media.get("id") or "")
        count = int(media.get("comments_count") or 0)
        next_counts[media_id] = count
        for c in rows:
            raw_comments += 1
            stamp = str(c.get("timestamp") or "")
            text = str(c.get("text") or "")
            who = c.get("username") or (c.get("from") or {}).get("username") or "?"
            if len(raw_samples) < 6:
                raw_samples.append(f"{stamp} @{who}: {text[:70]}")
            if _inbound_is_fresh(stamp):
                scanned_fresh += 1
                if len(fresh_samples) < 8:
                    fresh_samples.append(f"@{who}: {text[:80]}")
            item = _ingest_comment_row(c, media=media)
            if not item:
                continue
            synced.append(item)
            if (
                auto_reply
                and auto_reply_enabled()
                and item.get("status") == "pending"
                and _inbound_is_fresh(str(item.get("created_at") or ""))
            ):
                to_reply.append(str(item["id"]))

    _save_media_comment_counts(next_counts)
    for item_id in to_reply:
        try:
            await schedule_auto_reply(item_id)
        except Exception:  # noqa: BLE001
            logger.exception("Could not schedule auto-reply for synced comment %s", item_id)
    logger.info(
        "IG comment sync media=%s fetch=%s raw=%s fresh_seen=%s synced=%s",
        len(media_rows),
        len(to_fetch),
        raw_comments,
        scanned_fresh,
        len(synced),
    )
    # Stash debug for the HTTP sync endpoint.
    sync_recent_comments.last_debug = {  # type: ignore[attr-defined]
        "media_scanned": len(media_rows),
        "media_with_comments": len(with_comments),
        "fetched": len(to_fetch),
        "raw_comments": raw_comments,
        "fresh_seen": scanned_fresh,
        "synced": len(synced),
        "fresh_samples": fresh_samples,
        "raw_samples": raw_samples,
        "max_age_hours": COMMENT_MAX_AGE_SECONDS / 3600,
    }
    return synced


sync_recent_comments.last_debug = {}  # type: ignore[attr-defined]


async def sync_tagged_media_comments(*, auto_reply: bool = False) -> list[dict[str, Any]]:
    """Comments on OTHER people's posts/reels that tag or @mention the hotel.

    Own-media polling never sees these. Meta Mentions API is used to reply.
    """
    conn = _require_connection()
    ig_user_id = conn["ig_user_id"]
    token = conn["page_access_token"]
    media_by_id: dict[str, dict[str, Any]] = {}

    # 1) Photo/people tags + collaborator tags.
    try:
        tagged = await _graph_pages(
            f"/{ig_user_id}/tags",
            token,
            {
                "fields": (
                    "id,caption,timestamp,permalink,media_type,media_product_type,"
                    "comments_count,username"
                ),
                "limit": 50,
            },
            max_pages=2,
        )
        for row in tagged:
            mid = str(row.get("id") or "").strip()
            if mid:
                media_by_id[mid] = row
                watch_mention_media(mid)
    except InstagramError as exc:
        logger.warning("IG tags poll failed: %s", exc)

    # 2) Media ids from prior @mention webhooks (caption/comment mentions).
    for mid in _load_mention_watch():
        if mid in media_by_id:
            continue
        media_by_id[mid] = {"id": mid}

    # Prefer media with activity; always include watch-list ids.
    candidates = list(media_by_id.values())
    candidates.sort(
        key=lambda row: int(row.get("comments_count") or 0),
        reverse=True,
    )
    to_fetch = candidates[:40]

    synced: list[dict[str, Any]] = []
    to_reply: list[str] = []
    fresh_samples: list[str] = []
    interesting: list[str] = []
    tag_labels: list[str] = []

    for media in to_fetch[:12]:
        uname = str(media.get("username") or "")
        cap = str(media.get("caption") or "").replace("\n", " ")[:60]
        tag_labels.append(f"{media.get('id')} @{uname} {cap}")

    async def _load_one(
        media: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        mid = str(media.get("id") or "")
        rows = await _fetch_media_comments(mid, token)
        source = "comments"
        if not rows:
            rows = await _fetch_mentioned_media_comments(
                ig_user_id=str(ig_user_id),
                media_id=mid,
                token=token,
            )
            source = "mentioned_media" if rows else "empty"
        return media, rows, source

    loaded: list[tuple[dict[str, Any], list[dict[str, Any]], str]] = []
    for start in range(0, len(to_fetch), 6):
        chunk = to_fetch[start : start + 6]
        loaded.extend(await asyncio.gather(*[_load_one(m) for m in chunk]))

    sources: dict[str, int] = {}
    for media, rows, source in loaded:
        sources[source] = sources.get(source, 0) + 1
        for c in rows:
            text = str(c.get("text") or "")
            stamp = str(c.get("timestamp") or "")
            who = str(
                c.get("username") or (c.get("from") or {}).get("username") or "?"
            )
            lowered = text.lower()
            if (
                len(interesting) < 10
                and (
                    "valor" in lowered
                    or "precio" in lowered
                    or who.lower() == "emkrypted"
                    or "blackcathostal" in lowered
                )
            ):
                interesting.append(f"{stamp} @{who}: {text[:90]}")
            if _inbound_is_fresh(stamp) and len(fresh_samples) < 8:
                fresh_samples.append(f"@{who}: {text[:80]}")
            item = _ingest_comment_row(c, media=media, item_type="mention")
            if not item:
                continue
            synced.append(item)
            if (
                auto_reply
                and auto_reply_enabled()
                and item.get("status") == "pending"
            ):
                to_reply.append(str(item["id"]))

    for item_id in to_reply:
        try:
            await schedule_auto_reply(item_id)
        except Exception:  # noqa: BLE001
            logger.exception("Could not schedule reply for tagged-media comment %s", item_id)

    sync_tagged_media_comments.last_debug = {  # type: ignore[attr-defined]
        "tagged_or_watched": len(media_by_id),
        "fetched": len(to_fetch),
        "synced": len(synced),
        "fresh_samples": fresh_samples,
        "interesting_any_age": interesting,
        "tag_labels": tag_labels,
        "comment_sources": sources,
    }
    logger.info(
        "IG tagged/mention media sync watched=%s fetched=%s synced=%s",
        len(media_by_id),
        len(to_fetch),
        len(synced),
    )
    return synced


sync_tagged_media_comments.last_debug = {}  # type: ignore[attr-defined]


async def sync_collaborative_media_comments(
    *,
    auto_reply: bool = False,
) -> list[dict[str, Any]]:
    """Read comments on reels/posts where Black Cat is an accepted collaborator.

    These show as «blackcathostal y N personas más». Meta exposes them on
    GET /{ig-user-id}/collaborative_media (not always on /media alone).
    """
    conn = _require_connection()
    ig_user_id = conn["ig_user_id"]
    token = conn["page_access_token"]
    try:
        media_rows = await _graph_pages(
            f"/{ig_user_id}/collaborative_media",
            token,
            {
                "fields": (
                    "id,caption,timestamp,permalink,media_type,media_product_type,"
                    "comments_count,username,owner"
                ),
                "limit": 50,
            },
            max_pages=3,
        )
    except InstagramError as exc:
        logger.warning("collaborative_media poll failed: %s", exc)
        sync_collaborative_media_comments.last_debug = {  # type: ignore[attr-defined]
            "error": str(exc),
            "media": 0,
            "synced": 0,
        }
        return []

    prev_counts = _load_media_comment_counts()
    next_counts = dict(prev_counts)
    to_fetch: list[dict[str, Any]] = []
    for index, media in enumerate(media_rows):
        media_id = str(media.get("id") or "").strip()
        if not media_id:
            continue
        watch_mention_media(media_id)
        count = int(media.get("comments_count") or 0)
        previous = int(prev_counts.get(media_id, -1))
        if count <= 0:
            next_counts[media_id] = 0
            continue
        # Always check newest collabs + any whose comment count grew.
        if index < 20 or previous < 0 or count > previous:
            to_fetch.append(media)
        if len(to_fetch) >= 40:
            break

    synced: list[dict[str, Any]] = []
    to_reply: list[str] = []
    fresh_samples: list[str] = []

    async def _load_one(
        media: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        mid = str(media.get("id") or "")
        rows = await _fetch_media_comments(mid, token)
        if not rows:
            rows = await _fetch_mentioned_media_comments(
                ig_user_id=str(ig_user_id),
                media_id=mid,
                token=token,
            )
        return media, rows

    for start in range(0, len(to_fetch), 8):
        chunk = to_fetch[start : start + 8]
        loaded = await asyncio.gather(*[_load_one(m) for m in chunk])
        for media, rows in loaded:
            mid = str(media.get("id") or "")
            next_counts[mid] = int(media.get("comments_count") or 0)
            for c in rows:
                stamp = str(c.get("timestamp") or "")
                text = str(c.get("text") or "")
                who = c.get("username") or (c.get("from") or {}).get("username") or "?"
                if _inbound_is_fresh(stamp) and len(fresh_samples) < 8:
                    fresh_samples.append(f"@{who}: {text[:80]}")
                # Treat as own-grid comment (collaborator), not foreign UGC mention.
                item = _ingest_comment_row(
                    c,
                    media=media,
                    item_type="comment",
                    collaborative=True,
                )
                if not item:
                    continue
                synced.append(item)
                if (
                    auto_reply
                    and auto_reply_enabled()
                    and item.get("status") == "pending"
                ):
                    to_reply.append(str(item["id"]))

    _save_media_comment_counts(next_counts)
    for item_id in to_reply:
        try:
            await schedule_auto_reply(item_id)
        except Exception:  # noqa: BLE001
            logger.exception("Could not schedule collab-media reply %s", item_id)

    sync_collaborative_media_comments.last_debug = {  # type: ignore[attr-defined]
        "media": len(media_rows),
        "fetched": len(to_fetch),
        "synced": len(synced),
        "fresh_samples": fresh_samples,
    }
    logger.info(
        "IG collaborative_media sync media=%s fetched=%s synced=%s",
        len(media_rows),
        len(to_fetch),
        len(synced),
    )
    return synced


sync_collaborative_media_comments.last_debug = {}  # type: ignore[attr-defined]


def silence_stale_pending_public(max_age_seconds: float = COMMENT_MAX_AGE_SECONDS) -> int:
    """Mark pending comments/mentions older than 2h as skipped."""
    fixed = 0
    for item in list_inbox(status="pending"):
        if item.get("type") not in {"comment", "mention"}:
            continue
        if _inbound_is_fresh(str(item.get("created_at") or ""), max_age_seconds=max_age_seconds):
            continue
        item["status"] = "skipped"
        item["escalation_reason"] = "stale_over_2h"
        item["updated_at"] = _now_iso()
        upsert_inbox_item(item)
        fixed += 1
    return fixed


def silence_self_greeting_loop() -> int:
    """Stop the Mentions poller from answering our own public greeting forever."""
    fixed = 0
    for item in list_inbox():
        if item.get("type") not in {"comment", "mention"}:
            continue
        if not _looks_like_own_public_reply(str(item.get("text") or "")):
            continue
        if item.get("status") == "skipped" and item.get("escalation_reason") == "own_public_reply":
            continue
        item["status"] = "skipped"
        item["escalation_reason"] = "own_public_reply"
        item["updated_at"] = _now_iso()
        upsert_inbox_item(item)
        fixed += 1
    return fixed


async def schedule_pending_public_replies(limit: int = 25) -> int:
    """Kick auto-reply for pending comments/mentions from the last 2 hours only."""
    if not auto_reply_enabled():
        return 0
    silence_stale_pending_public()
    silence_self_greeting_loop()
    queued = 0
    pending = list_inbox(status="pending")
    for item in pending:
        if item.get("type") not in {"comment", "mention"}:
            continue
        if _looks_like_own_public_reply(str(item.get("text") or "")):
            continue
        if not _inbound_is_fresh(str(item.get("created_at") or "")):
            continue
        try:
            await schedule_auto_reply(str(item.get("id") or ""))
            queued += 1
        except Exception:  # noqa: BLE001
            logger.exception("Could not schedule pending public reply %s", item.get("id"))
        if queued >= limit:
            break
    return queued


async def poll_instagram_comments_loop() -> None:
    """Meta does not webhook Reel comments — poll Graph often when auto-reply is on."""
    await asyncio.sleep(5.0)
    tick = 0
    while True:
        try:
            if load_connection() and auto_reply_enabled():
                tick += 1
                silence_self_greeting_loop()
                deep = tick % 3 == 0
                await sync_recent_comments(
                    limit_media=(
                        COMMENT_SYNC_MEDIA_LIMIT if deep else COMMENT_POLL_MEDIA_LIMIT
                    ),
                    auto_reply=True,
                    fetch_budget=(
                        COMMENT_FETCH_BUDGET_SYNC if deep else COMMENT_FETCH_BUDGET
                    ),
                    rotate_offset=tick * 17,
                )
                # Collab reels («blackcathostal y N personas más»).
                await sync_collaborative_media_comments(auto_reply=True)
                # Others' reels that @mention / tag the hotel (UGC without collab).
                await sync_tagged_media_comments(auto_reply=True)
                await schedule_pending_public_replies()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Instagram comment poll failed")
        await asyncio.sleep(COMMENT_POLL_INTERVAL_SECONDS)


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
            # Prefer the guest IGSID as thread_id so webhook + sync share one chain.
            canonical_thread = guest_id or thread_id or sender_id
            item = upsert_inbox_item(
                {
                    "id": str(uuid.uuid4()),
                    "type": "message",
                    "thread_id": canonical_thread,
                    "meta_conversation_id": thread_id or None,
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
    """Remove map/directions links from DM text. The map goes as an image bubble instead."""
    cleaned = re.sub(
        r"https?://(?:www\.)?(?:google\.com/maps|maps\.googleapis\.com|maps\.google\.com|goo\.gl/maps|maps\.app\.goo\.gl)\S*",
        "",
        text or "",
    )
    cleaned = re.sub(
        r"https?://(?:api\.)?blackcathostal\.com/api/g/[a-z0-9]+\S*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"https?://bit\.ly/\S+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?im)^\s*(ver en google maps|ver mapa|open map)\s*$", "", cleaned)
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
    from app.services.deepseek_instagram import (
        enforce_single_short_reply,
        sanitize_outbound_message,
    )

    text = enforce_single_short_reply(sanitize_outbound_message(_strip_maps_urls(message or "")))
    if not text:
        raise InstagramError("Mensaje vacío")
    # Only attach a map when this turn asked for one. Never reuse a stale pending image
    # on WhatsApp/price replies (that made IG look “confused”: image first, then text).
    image = str(map_image_url or "").strip()
    if not image and source == "human":
        image = str(item.get("pending_map_image") or "").strip()
    open_url = str(maps_url or "").strip()
    if not open_url and source == "human":
        open_url = str(item.get("pending_maps_url") or "").strip()
    # Tap target for the map card only (never shown as plain text in the first bubble).
    if open_url and not (
        "google.com/maps" in open_url
        or "/api/g/" in open_url
        or "bit.ly/" in open_url
    ):
        open_url = ""

    try:
        meta_result: dict[str, Any]
        if item.get("type") == "mention":
            media_id = str(item.get("media_id") or item.get("thread_id") or "").strip()
            if not media_id:
                raise InstagramError("Falta media_id para responder la mención")
            # Mentions API only threads under a comment if WE were @mentioned in it.
            # Guests often just write "hola valor" — then reply at media level.
            own = _own_instagram_username()
            guest_text = str(item.get("text") or "").lower()
            threaded = bool(own and f"@{own.lower()}" in guest_text)
            comment_id = (
                str(item.get("meta_comment_id") or "").strip() if threaded else ""
            )
            try:
                meta_result = await reply_to_mention(
                    media_id=media_id,
                    message=text,
                    comment_id=comment_id or None,
                )
            except InstagramError:
                if comment_id:
                    meta_result = await reply_to_mention(
                        media_id=media_id,
                        message=text,
                        comment_id=None,
                    )
                else:
                    raise
        elif item.get("type") == "comment":
            comment_id = item.get("meta_comment_id")
            if not comment_id:
                raise InstagramError("Falta meta_comment_id")
            try:
                meta_result = await reply_to_comment(comment_id, text)
            except InstagramError:
                # Collaborative media: sometimes only Mentions API can publish the reply.
                if item.get("collaborative"):
                    media_id = str(item.get("media_id") or item.get("thread_id") or "")
                    if not media_id:
                        raise
                    meta_result = await reply_to_mention(
                        media_id=media_id,
                        message=text,
                        comment_id=str(comment_id),
                    )
                else:
                    raise
        else:
            recipient_id = item.get("author_id") or item.get("peer_id")
            if not recipient_id:
                raise InstagramError("Falta author_id (IGSID) para DM")
            # Text first (no URL). Then Google map card — tap opens Google Maps.
            meta_result = await send_dm(recipient_id, text)
            if image.startswith("https://"):
                try:
                    img = await send_dm_image(
                        recipient_id,
                        image,
                        open_url=open_url if open_url.startswith("https://") else "",
                    )
                    replies = list(item.get("replies") or [])
                    replies.append(
                        {
                            "text": "",
                            "source": source,
                            "created_at": _now_iso(),
                            "meta_message_id": img.get("message_id") or img.get("id") or "",
                            "is_map": True,
                            "image_url": image,
                            "maps_url": open_url,
                            "map_card": img.get("_map_card") or "image",
                        }
                    )
                    item["replies"] = replies
                    item.pop("map_send_error", None)
                except InstagramError as map_exc:
                    # Keep the raw Meta detail — never mask map failures as 24h-window.
                    detail = ""
                    if isinstance(map_exc.details, dict):
                        err = map_exc.details.get("error") or map_exc.details
                        if isinstance(err, dict):
                            detail = str(err.get("message") or "")
                    item["map_send_error"] = detail or str(map_exc)
                    item["pending_map_image"] = image
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
    # Order local history: text → map image.
    extras: list[dict[str, Any]] = []
    while replies and replies[-1].get("is_map"):
        extras.insert(0, replies.pop())
    replies.append(reply)
    replies.extend(extras)
    item["replies"] = replies
    item["status"] = "replied_deepseek" if source == "deepseek" else "replied_human"
    item["updated_at"] = _now_iso()
    item.pop("pending_map_image", None)
    item.pop("pending_maps_url", None)
    return upsert_inbox_item(item)


def _thread_debounce_key(item: dict[str, Any]) -> str:
    # Comments/mentions: one timer each (never merge with other guests on the post).
    if item.get("type") in {"comment", "mention"}:
        prefix = "mention" if item.get("type") == "mention" else "comment"
        return (
            f"{prefix}:"
            f"{item.get('id') or item.get('external_id') or item.get('meta_comment_id') or ''}"
        )
    return str(item.get("peer_id") or item.get("thread_id") or item.get("id") or "")


def _parse_iso(stamp: str | None) -> datetime | None:
    raw = str(stamp or "").strip()
    if not raw:
        return None
    # Instagram Graph often returns +0000; fromisoformat wants +00:00.
    raw = raw.replace("Z", "+00:00")
    raw = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", raw)
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _pending_message_burst(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Inbound pending bubbles in this DM that belong to the current typing burst."""
    if item.get("type") != "message":
        return [item]
    related = _related_dm_items(item)
    last_out: datetime | None = None
    for row in related:
        if str(row.get("direction") or "in") == "out":
            stamp = _parse_iso(row.get("created_at"))
            if stamp and (last_out is None or stamp > last_out):
                last_out = stamp
        for reply in row.get("replies") or []:
            stamp = _parse_iso(reply.get("created_at"))
            if stamp and (last_out is None or stamp > last_out):
                last_out = stamp

    burst: list[dict[str, Any]] = []
    for row in related:
        if str(row.get("direction") or "in") != "in":
            continue
        if row.get("status") not in {"pending", "batched"}:
            continue
        text = str(row.get("text") or "").strip()
        if not text or text == "[archivo]":
            continue
        stamp = _parse_iso(row.get("created_at"))
        if last_out and stamp and stamp <= last_out:
            continue
        burst.append(row)
    if not burst:
        return [item] if item.get("status") == "pending" else []
    burst.sort(key=lambda row: str(row.get("created_at") or ""))
    # Keep messages that arrived close together (same burst), drop older pending leftovers.
    window_sec = AUTO_REPLY_DEBOUNCE_SECONDS * 4
    newest = _parse_iso(burst[-1].get("created_at"))
    if newest:
        tight: list[dict[str, Any]] = []
        for row in reversed(burst):
            stamp = _parse_iso(row.get("created_at"))
            if stamp and (newest - stamp).total_seconds() > window_sec:
                break
            tight.append(row)
        burst = list(reversed(tight))
    return burst or [item]


def _combined_burst_text(burst: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in burst:
        text = str(row.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _seconds_since_last_help_reply(item: dict[str, Any]) -> float | None:
    """How many seconds since we last sent the short greeting/help offer."""
    newest: datetime | None = None
    for row in _related_dm_items(item):
        for reply in row.get("replies") or []:
            text = str(reply.get("text") or "").strip().lower()
            if not any(marker in text for marker in _HELP_OFFER_MARKERS):
                continue
            stamp = _parse_iso(reply.get("created_at"))
            if not stamp:
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if newest is None or stamp > newest:
                newest = stamp
    if newest is None:
        return None
    now = datetime.now(timezone.utc)
    return max(0.0, (now - newest).total_seconds())


async def schedule_auto_reply(item_id: str) -> None:
    """Debounce auto-reply so multi-bubble typing gets one answer."""
    item = get_inbox_item(item_id)
    if not item or item.get("status") != "pending":
        return
    if item.get("type") in {"comment", "mention"} and _is_own_instagram_actor(
        author_id=str(item.get("author_id") or ""),
        author_username=str(item.get("author_username") or ""),
    ):
        item["status"] = "skipped"
        item["escalation_reason"] = "own_comment"
        item["updated_at"] = _now_iso()
        upsert_inbox_item(item)
        return
    key = _thread_debounce_key(item) or item_id
    now = time.monotonic()
    async with _auto_reply_debounce_lock:
        deadline = _auto_reply_debounce_deadline.get(key)
        if deadline is None:
            deadline = now + AUTO_REPLY_MAX_BURST_SECONDS
            _auto_reply_debounce_deadline[key] = deadline
        delay = min(AUTO_REPLY_DEBOUNCE_SECONDS, max(0.3, deadline - now))
        previous = _auto_reply_debounce_tasks.get(key)
        if previous and not previous.done():
            previous.cancel()
        _auto_reply_debounce_tasks[key] = asyncio.create_task(
            _run_debounced_auto_reply(key, item_id, delay),
            name=f"ig-auto-reply-{key}",
        )


async def _run_debounced_auto_reply(key: str, item_id: str, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    try:
        await run_auto_reply(item_id)
    except Exception:  # noqa: BLE001 — webhook path already logged via item status
        logger.exception("Debounced Instagram auto-reply failed for %s", item_id)
    finally:
        async with _auto_reply_debounce_lock:
            current = _auto_reply_debounce_tasks.get(key)
            if current is asyncio.current_task():
                _auto_reply_debounce_tasks.pop(key, None)
                _auto_reply_debounce_deadline.pop(key, None)


async def run_auto_reply(item_id: str) -> dict[str, Any]:
    item = get_inbox_item(item_id)
    if not item:
        raise InstagramError("Item de inbox no encontrado")

    peer_key = _thread_debounce_key(item) or item_id
    peer_lock = _auto_reply_peer_locks.setdefault(peer_key, asyncio.Lock())
    if peer_lock.locked():
        # Another reply for this chat is already running — avoid a second bubble.
        item["status"] = "skipped"
        item["escalation_reason"] = "peer_reply_in_flight"
        item["updated_at"] = _now_iso()
        return upsert_inbox_item(item)

    async with peer_lock:
        return await _run_auto_reply_locked(item_id)


def _foreign_reel_dm_redirect(inbound_text: str) -> str:
    sample = (inbound_text or "").lower()
    if re.search(r"\b(hello|hi|price|room|book)\b", sample):
        return _FOREIGN_REEL_DM_REDIRECT["en"]
    if re.search(r"\b(oi|olá|ola|preço|preco|quarto)\b", sample):
        return _FOREIGN_REEL_DM_REDIRECT["pt"]
    return _FOREIGN_REEL_DM_REDIRECT["es"]


def _already_sent_foreign_redirect(item: dict[str, Any]) -> bool:
    """One 'escríbenos por interno' per guest on the same foreign post."""
    media_id = str(item.get("media_id") or item.get("thread_id") or "")
    author = str(item.get("author_id") or item.get("author_username") or "")
    if not media_id:
        return False
    for row in list_inbox():
        if row.get("id") == item.get("id"):
            continue
        if str(row.get("media_id") or row.get("thread_id") or "") != media_id:
            continue
        same_author = author and (
            str(row.get("author_id") or "") == author
            or str(row.get("author_username") or "") == author
        )
        if not same_author:
            continue
        for reply in row.get("replies") or []:
            if _looks_like_own_public_reply(str(reply.get("text") or "")):
                return True
        if _looks_like_own_public_reply(str(row.get("text") or "")):
            return True
    return False


async def _run_auto_reply_locked(item_id: str) -> dict[str, Any]:
    item = get_inbox_item(item_id)
    if not item:
        raise InstagramError("Item de inbox no encontrado")

    if item.get("type") in {"comment", "mention"} and _is_own_instagram_actor(
        author_id=str(item.get("author_id") or ""),
        author_username=str(item.get("author_username") or ""),
    ):
        item["status"] = "skipped"
        item["escalation_reason"] = "own_comment"
        item["updated_at"] = _now_iso()
        return upsert_inbox_item(item)

    burst = _pending_message_burst(item)
    if not burst:
        item["status"] = "skipped"
        item["escalation_reason"] = "no_pending_burst"
        item["updated_at"] = _now_iso()
        return upsert_inbox_item(item)

    latest = burst[-1]
    inbound = _combined_burst_text(burst) or str(latest.get("text") or "")
    burst_texts = {str(row.get("text") or "").strip() for row in burst if str(row.get("text") or "").strip()}

    # Reels/posts of OTHER accounts: never dump prices publicly — send to DM.
    if latest.get("type") == "mention" or latest.get("foreign_media"):
        if not latest.get("meta_comment_id"):
            latest["status"] = "skipped"
            latest["escalation_reason"] = "caption_mention_only"
            latest["updated_at"] = _now_iso()
            return upsert_inbox_item(latest)
        if _already_sent_foreign_redirect(latest):
            latest["status"] = "skipped"
            latest["escalation_reason"] = "foreign_redirect_already_sent"
            latest["updated_at"] = _now_iso()
            return upsert_inbox_item(latest)
        for row in burst[:-1]:
            row["status"] = "batched"
            row["escalation_reason"] = "merged_burst"
            row["updated_at"] = _now_iso()
            upsert_inbox_item(row)
        return await send_inbox_reply(
            str(latest.get("id") or item_id),
            _foreign_reel_dm_redirect(inbound),
            source="deepseek",
        )

    try:
        db = SessionLocal()
        try:
            history = thread_history_for_reply(latest, inbound_text=inbound)
            # Drop the burst bubbles from history; they are the current inbound text.
            trimmed: list[dict[str, str]] = []
            for turn in history:
                if (
                    turn.get("role") == "user"
                    and str(turn.get("content") or "").strip() in burst_texts
                ):
                    continue
                trimmed.append(turn)
            recent_help = _seconds_since_last_help_reply(latest)
            decision = await generate_instagram_reply(
                db=db,
                inbound_text=inbound,
                item_type=latest.get("type") or item.get("type") or "message",
                author_username=latest.get("author_username") or item.get("author_username"),
                person_history=author_channel_history(
                    author_username=latest.get("author_username") or item.get("author_username"),
                    author_id=latest.get("author_id") or item.get("author_id"),
                    exclude_id=latest.get("id"),
                ),
                thread_history=trimmed,
                skip_duplicate_greeting=(
                    recent_help is not None and recent_help < GREETING_SKIP_WINDOW_SECONDS
                ),
            )
        finally:
            db.close()
    except (DeepSeekConfigError, DeepSeekError) as exc:
        for row in burst:
            row["status"] = "failed"
            row["escalation_reason"] = str(exc)
            row["updated_at"] = _now_iso()
            upsert_inbox_item(row)
        raise

    # Mark earlier bubbles in the same burst so they do not fire another reply.
    for row in burst[:-1]:
        row["status"] = "batched"
        row["escalation_reason"] = "merged_burst"
        row["updated_at"] = _now_iso()
        upsert_inbox_item(row)

    if decision.get("action") == "skip":
        latest["status"] = "skipped"
        latest["escalation_reason"] = decision.get("reason") or "skip"
        latest["updated_at"] = _now_iso()
        return upsert_inbox_item(latest)

    if decision["action"] == "escalate":
        latest["status"] = "needs_human"
        latest["escalation_reason"] = decision.get("reason") or "escalate"
        if decision.get("message"):
            latest["deepseek_draft"] = decision["message"]
        latest["updated_at"] = _now_iso()
        return upsert_inbox_item(latest)

    return await send_inbox_reply(
        str(latest.get("id") or item_id),
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
    if maps_url.startswith("https://") and (
        "google.com/maps" in maps_url or "/api/g/" in maps_url or "bit.ly/" in maps_url
    ):
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


def _related_comment_items(item: dict[str, Any]) -> list[dict[str, Any]]:
    """All comments from the same guest on the same post (one public chain)."""
    if item.get("type") != "comment":
        return [item]
    key = _comment_group_key(item)
    related = [
        row
        for row in list_inbox(item_type="comment")
        if _comment_group_key(row) == key
    ]
    related.sort(key=lambda row: str(row.get("created_at") or ""))
    return related or [item]


def conversation_turns(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Chronological bubbles for the admin chat."""
    if item.get("type") != "message":
        turns: list[dict[str, Any]] = []
        for row in _related_comment_items(item):
            turns.append(
                {
                    "role": "in",
                    "text": row.get("text") or "",
                    "created_at": row.get("created_at"),
                    "author": row.get("author_username") or row.get("author_id") or "",
                    "item_id": row.get("id"),
                    "reply_index": None,
                    "status": row.get("status"),
                }
            )
            for index, reply in enumerate(row.get("replies") or []):
                turns.append(
                    {
                        "role": "out",
                        "text": reply.get("text") or "",
                        "created_at": reply.get("created_at"),
                        "source": reply.get("source") or "human",
                        "item_id": row.get("id"),
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


def _own_instagram_username() -> str:
    conn = load_connection() or {}
    return str(conn.get("ig_username") or "").strip().lstrip("@").lower()


def _is_own_instagram_actor(
    *,
    author_id: str | None = None,
    author_username: str | None = None,
) -> bool:
    """True when the author is the hostel Instagram / Page (never auto-reply)."""
    own_ids = _own_instagram_ids()
    aid = str(author_id or "").strip()
    if aid and aid in own_ids:
        return True
    own_user = _own_instagram_username()
    username = str(author_username or "").strip().lstrip("@").lower()
    return bool(own_user and username and username == own_user)


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


async def _store_reaction_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Guest ❤️/👍 on a DM. Store in the thread; never auto-reply to a reaction."""
    reaction = event.get("reaction")
    if not isinstance(reaction, dict):
        return None
    own_ids = _own_instagram_ids()
    sender = str((event.get("sender") or {}).get("id") or "")
    recipient = str((event.get("recipient") or {}).get("id") or "")
    if not sender or sender in own_ids:
        return None
    guest_id = sender
    if recipient and recipient not in own_ids and recipient != guest_id:
        # Unexpected shape — keep the sender as the guest.
        pass
    action = str(reaction.get("action") or "react").strip().lower() or "react"
    emoji = str(reaction.get("emoji") or "").strip()
    kind = str(reaction.get("reaction") or "").strip()
    mid = str(reaction.get("mid") or "").strip()
    label = emoji or kind or "❤️"
    if action == "unreact":
        text = f"Quitó la reacción {label}".strip()
    else:
        text = f"{label} (reacción)"
    external = f"reaction:{mid or 'unknown'}:{action}:{guest_id}:{label}"
    username = await _instagram_username(guest_id)
    item = ingest_inbound(
        item_type="message",
        external_id=external,
        text=text,
        author_id=guest_id,
        author_username=username,
        thread_id=guest_id,
        peer_id=guest_id,
        direction="in",
    )
    if username and not item.get("author_username"):
        item["author_username"] = username
    # Reactions are signals, not questions — never trigger DeepSeek.
    if item.get("status") == "pending":
        item["status"] = "skipped"
        item["escalation_reason"] = "reaction"
        item["updated_at"] = _now_iso()
        item = upsert_inbox_item(item)
    return item


async def _store_dm_event(event: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(event.get("reaction"), dict):
        return await _store_reaction_event(event)
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
        # Do not reply on every bubble; wait for the guest to finish typing.
        await schedule_auto_reply(item["id"])
    return item


def silence_pending_own_comments() -> int:
    """Stop any already-queued self-replies (from the comment reply loop)."""
    fixed = 0
    for item in list_inbox():
        if item.get("type") != "comment" or item.get("status") != "pending":
            continue
        if not _is_own_instagram_actor(
            author_id=str(item.get("author_id") or ""),
            author_username=str(item.get("author_username") or ""),
        ):
            continue
        item["status"] = "skipped"
        item["escalation_reason"] = "own_comment"
        item["updated_at"] = _now_iso()
        upsert_inbox_item(item)
        fixed += 1
    return fixed


async def process_webhook_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse Meta webhook and auto-reply when enabled."""
    # Break any leftover self-reply queue queued before the own-comment filter.
    try:
        silence_pending_own_comments()
    except Exception:  # noqa: BLE001
        logger.exception("Could not silence pending own Instagram comments")
    results: list[dict[str, Any]] = []
    dm_fields = {
        "messages",
        "message_echoes",
        "messaging_postbacks",
        "message_reactions",
    }
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
            ) or isinstance(value.get("reaction"), dict):
                item = await _store_dm_event(value)
                if item:
                    results.append(item)
                continue

            # @mention on someone else's caption/comment (reels/posts). Webhook
            # only sends media_id + optional comment_id — no text/from.
            if field == "mentions":
                media_id = str(value.get("media_id") or "").strip()
                comment_id = str(value.get("comment_id") or "").strip()
                if not media_id and not comment_id:
                    continue
                # Keep watching this foreign media for later guest questions (precio, etc.).
                watch_mention_media(media_id)
                try:
                    details = await fetch_mention_details(
                        media_id=media_id or None,
                        comment_id=comment_id or None,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Could not enrich Instagram mention webhook")
                    details = {
                        "text": "",
                        "author_id": "",
                        "author_username": "",
                        "media_id": media_id,
                        "comment_id": comment_id,
                    }
                media_id = str(details.get("media_id") or media_id)
                comment_id = str(details.get("comment_id") or comment_id)
                text = str(details.get("text") or "").strip()
                author_id = str(details.get("author_id") or "")
                author_username = str(details.get("author_username") or "")
                if _is_own_instagram_actor(
                    author_id=author_id,
                    author_username=author_username,
                ):
                    continue
                if not media_id:
                    continue
                external = (
                    f"mention:{comment_id}"
                    if comment_id
                    else f"mention:media:{media_id}"
                )
                item = ingest_inbound(
                    item_type="mention",
                    external_id=external,
                    text=text or "(mención en reel/post)",
                    author_id=author_id or None,
                    author_username=author_username or None,
                    thread_id=media_id,
                    meta_comment_id=comment_id or None,
                    media_id=media_id,
                )
                if auto_reply_enabled() and item.get("status") == "pending":
                    try:
                        await schedule_auto_reply(item["id"])
                    except Exception as exc:  # noqa: BLE001
                        item["status"] = "failed"
                        item["escalation_reason"] = str(exc)
                        upsert_inbox_item(item)
                results.append(item)
                continue

            if field not in {"comments", "live_comments"} and "text" not in value:
                continue
            comment_id = value.get("id") or value.get("comment_id")
            text = value.get("text") or ""
            if isinstance(text, dict):
                continue
            from_user = value.get("from") or {}
            media = value.get("media") or {}
            if not comment_id and not str(text).strip():
                continue
            author_id = str(from_user.get("id") or value.get("user_id") or "")
            author_username = str(
                from_user.get("username") or value.get("username") or ""
            )
            # Critical: Instagram also webhooks OUR public replies. Without this
            # filter the bot answers itself in a loop under the same post.
            if _is_own_instagram_actor(
                author_id=author_id,
                author_username=author_username,
            ):
                continue
            # Guest comments (top-level OR nested replies to our answer) can get
            # auto-reply. Own comments are already filtered above — that is what
            # stops the self-reply loop, not skipping every nested guest message.
            parent_id = str(value.get("parent_id") or "").strip()
            external = f"comment:{comment_id or uuid.uuid4()}"
            item = ingest_inbound(
                item_type="comment",
                external_id=external,
                text=str(text),
                author_id=author_id or None,
                author_username=author_username or None,
                thread_id=media.get("id") or value.get("media_id"),
                meta_comment_id=comment_id,
                media_id=media.get("id") or value.get("media_id"),
            )
            if parent_id:
                item["parent_comment_id"] = parent_id
                item = upsert_inbox_item(item)
            if auto_reply_enabled() and item.get("status") == "pending":
                try:
                    await schedule_auto_reply(item["id"])
                except Exception as exc:  # noqa: BLE001
                    item["status"] = "failed"
                    item["escalation_reason"] = str(exc)
                    upsert_inbox_item(item)
            results.append(item)
    return results
