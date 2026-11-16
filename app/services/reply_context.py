"""Per-reply context for Instagram/Facebook agent tools."""

from __future__ import annotations

from contextvars import ContextVar

_reply_username: ContextVar[str] = ContextVar("reply_username", default="")
_reply_channel: ContextVar[str] = ContextVar("reply_channel", default="instagram")


def set_reply_context(*, username: str | None, channel: str | None) -> None:
    _reply_username.set(str(username or "").strip().lower().lstrip("@"))
    ch = str(channel or "instagram").strip().lower() or "instagram"
    if ch in {"ig"}:
        ch = "instagram"
    if ch in {"fb", "messenger"}:
        ch = "facebook"
    _reply_channel.set(ch)


def get_reply_username() -> str:
    return _reply_username.get()


def get_reply_channel() -> str:
    return _reply_channel.get() or "instagram"
