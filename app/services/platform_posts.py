"""Posts created from the admin, for Instagram and Facebook."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings

LOG_FILE = settings.uploads_dir / "cache" / "platform_publications.json"
_lock = threading.Lock()


def _read() -> list[dict[str, Any]]:
    if not LOG_FILE.is_file():
        return []
    try:
        data = json.loads(LOG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write(rows: list[dict[str, Any]]) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(json.dumps(rows[:300], ensure_ascii=False, indent=2), encoding="utf-8")


def list_publications(network: str | None = None) -> list[dict[str, Any]]:
    rows = _read()
    if network:
        key = network.strip().lower()
        rows = [row for row in rows if str(row.get("network") or "").lower() == key]
    return rows


def record_publication(
    *,
    network: str,
    kind: str,
    text: str,
    external_id: str = "",
    permalink: str = "",
) -> dict[str, Any]:
    row = {
        "id": uuid.uuid4().hex[:12],
        "network": network,
        "kind": kind,
        "text": (text or "").strip(),
        "external_id": external_id or "",
        "permalink": permalink or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        rows = [row, *_read()]
        _write(rows)
    return row


def update_publication(external_id: str, *, text: str | None = None, permalink: str | None = None) -> dict[str, Any] | None:
    code = str(external_id or "").strip()
    if not code:
        return None
    with _lock:
        rows = _read()
        for row in rows:
            if str(row.get("external_id") or "") != code:
                continue
            if text is not None:
                row["text"] = text.strip()
            if permalink is not None:
                row["permalink"] = permalink
            _write(rows)
            return row
    return None


def remove_publication(external_id: str) -> bool:
    code = str(external_id or "").strip()
    if not code:
        return False
    with _lock:
        rows = _read()
        next_rows = [row for row in rows if str(row.get("external_id") or "") != code]
        if len(next_rows) == len(rows):
            return False
        _write(next_rows)
    return True
