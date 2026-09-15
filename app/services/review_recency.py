from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

# Show only reviews published within the last calendar month (~30 days).
REVIEW_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

_RELATIVE_PATTERNS = (
    # Spanish
    (re.compile(r"hace\s+(\d+)\s+min", re.I), 60),
    (re.compile(r"hace\s+(\d+)\s+hora", re.I), 3600),
    (re.compile(r"hace\s+(\d+)\s+d[ií]a", re.I), 86400),
    (re.compile(r"hace\s+(\d+)\s+semana", re.I), 7 * 86400),
    (re.compile(r"hace\s+un[ao]?\s+semana", re.I), 7 * 86400),
    (re.compile(r"hace\s+(\d+)\s+mes", re.I), 30 * 86400),
    (re.compile(r"hace\s+un\s+mes", re.I), 30 * 86400),
    (re.compile(r"hace\s+(\d+)\s+a[nñ]o", re.I), 365 * 86400),
    # English
    (re.compile(r"(\d+)\s+minute", re.I), 60),
    (re.compile(r"an?\s+hour\s+ago|(\d+)\s+hour", re.I), 3600),
    (re.compile(r"a\s+day\s+ago|(\d+)\s+day", re.I), 86400),
    (re.compile(r"a\s+week\s+ago|(\d+)\s+week", re.I), 7 * 86400),
    (re.compile(r"a\s+month\s+ago|(\d+)\s+month", re.I), 30 * 86400),
    (re.compile(r"a\s+year\s+ago|(\d+)\s+year", re.I), 365 * 86400),
    # Portuguese
    (re.compile(r"h[aá]\s+(\d+)\s+min", re.I), 60),
    (re.compile(r"h[aá]\s+(\d+)\s+hora", re.I), 3600),
    (re.compile(r"h[aá]\s+(\d+)\s+dia", re.I), 86400),
    (re.compile(r"h[aá]\s+(\d+)\s+semana", re.I), 7 * 86400),
    (re.compile(r"h[aá]\s+(\d+)\s+m[eê]s", re.I), 30 * 86400),
    (re.compile(r"h[aá]\s+um\s+m[eê]s", re.I), 30 * 86400),
)


def parse_review_timestamp(item: dict[str, Any]) -> int:
    """Normalize Google/Tripadvisor review timestamps to unix seconds."""
    raw_time = item.get("time")
    if raw_time not in (None, "", 0, "0"):
        try:
            value = int(raw_time)
            if value > 10_000_000_000:
                value //= 1000
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass

    for key in ("publishTime", "publishedDate", "published_date", "published_at"):
        raw = item.get(key)
        if not raw:
            continue
        if isinstance(raw, (int, float)):
            value = int(raw)
            if value > 10_000_000_000:
                value //= 1000
            if value > 0:
                return value
        try:
            text = str(raw).strip().replace("Z", "+00:00")
            return int(datetime.fromisoformat(text).timestamp())
        except ValueError:
            continue

    relative = str(
        item.get("relative_time_description")
        or item.get("relativePublishTimeDescription")
        or ""
    ).strip()
    estimated = _estimate_from_relative(relative)
    if estimated:
        return estimated

    return 0


def _estimate_from_relative(text: str) -> int:
    if not text:
        return 0
    normalized = text.strip().lower()
    if normalized in {"hoy", "today", "agora", "ahora", "recién", "recently"}:
        return int(time.time()) - 3600

    for pattern, unit in _RELATIVE_PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue
        amount = 1
        for group in match.groups():
            if group and str(group).isdigit():
                amount = int(group)
                break
        # "a month ago" / "hace 1 mes" counts as within the last month window.
        age = amount * unit
        return int(time.time()) - age

    return 0


def is_recent_review(
    review: dict[str, Any],
    *,
    max_age_seconds: int = REVIEW_MAX_AGE_SECONDS,
    now: int | None = None,
) -> bool:
    now = int(now if now is not None else time.time())
    cutoff = now - max_age_seconds
    stamp = int(review.get("time") or 0)
    if stamp <= 0:
        stamp = parse_review_timestamp(review)
    return stamp >= cutoff


def filter_recent_reviews(
    reviews: list[dict[str, Any]],
    *,
    max_age_seconds: int = REVIEW_MAX_AGE_SECONDS,
) -> list[dict[str, Any]]:
    now = int(time.time())
    recent: list[dict[str, Any]] = []
    for review in reviews:
        stamp = int(review.get("time") or 0)
        if stamp <= 0:
            stamp = parse_review_timestamp(review)
            if stamp > 0:
                review = {**review, "time": stamp}
        if stamp >= now - max_age_seconds:
            recent.append(review)

    recent.sort(key=lambda item: int(item.get("time") or 0), reverse=True)
    return recent
