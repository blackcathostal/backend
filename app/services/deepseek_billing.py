from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.config import settings


async def get_deepseek_balance() -> dict[str, Any] | None:
    """Read the current DeepSeek account balance without exposing the API key."""
    if not settings.deepseek_api_key.strip():
        return None
    try:
        async with httpx.AsyncClient(
            base_url=settings.deepseek_base_url.rstrip("/"),
            timeout=settings.deepseek_timeout_seconds,
        ) as client:
            response = await client.get(
                "/user/balance",
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None

    balances = {}
    for item in payload.get("balance_infos", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        currency = str(item.get("currency") or "").upper()
        try:
            total = Decimal(str(item.get("total_balance")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        balances[currency] = float(total)
    return {
        "is_available": bool(payload.get("is_available")) if isinstance(payload, dict) else False,
        "balances": balances,
        "usd": balances.get("USD"),
    }


def calculate_platform_cost(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> float | None:
    if not before or not after:
        return None
    previous = before.get("usd")
    current = after.get("usd")
    if previous is None or current is None:
        return None
    cost = max(float(previous) - float(current), 0.0)
    # A zero delta usually means the balance endpoint rounded both snapshots
    # to the same value; it does not prove that a token-consuming request was free.
    return round(cost, 8) if cost > 0 else None
