from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings


class DeepSeekError(RuntimeError):
    pass


def _pricing() -> tuple[float, float, float]:
    if settings.deepseek_pricing_mode.lower() == "offpeak":
        return (
            settings.deepseek_offpeak_cache_hit_price_per_million,
            settings.deepseek_offpeak_cache_miss_price_per_million,
            settings.deepseek_offpeak_output_price_per_million,
        )
    return (
        settings.deepseek_cache_hit_price_per_million,
        settings.deepseek_cache_miss_price_per_million,
        settings.deepseek_output_price_per_million,
    )


def calculate_cost(usage: dict[str, Any]) -> float:
    hit_price, miss_price, output_price = _pricing()
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    miss = int(usage.get("prompt_cache_miss_tokens") or 0)
    prompt = int(usage.get("prompt_tokens") or 0)
    if not hit and not miss and prompt:
        miss = prompt
    output = int(usage.get("completion_tokens") or 0)
    return round(
        (hit * hit_price + miss * miss_price + output * output_price) / 1_000_000,
        8,
    )


def normalize_usage(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("usage") or {}
    hit = int(raw.get("prompt_cache_hit_tokens") or 0)
    miss = int(raw.get("prompt_cache_miss_tokens") or 0)
    prompt = int(raw.get("prompt_tokens") or hit + miss)
    completion = int(raw.get("completion_tokens") or 0)
    total = int(raw.get("total_tokens") or prompt + completion)
    normalized = {
        "prompt_tokens": prompt,
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    normalized["estimated_cost_usd"] = calculate_cost(normalized)
    normalized["pricing"] = _pricing()
    return normalized


def _extract_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise DeepSeekError("DeepSeek no devolvió ninguna respuesta.")
    content = choices[0].get("message", {}).get("content")
    if isinstance(content, list):
        content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    if not isinstance(content, str) or not content.strip():
        raise DeepSeekError("DeepSeek devolvió contenido vacío.")
    return content.strip()


async def generate_article(source_context: str) -> tuple[str, dict[str, Any]]:
    if not settings.deepseek_api_key.strip():
        raise DeepSeekError("DEEPSEEK_API_KEY no está configurada en el backend.")

    system_prompt = (
        "Eres el editor de turismo de Black Cat Hostal Boutique en Santiago de Chile. "
        "Usa únicamente los datos incluidos en FUENTES. No inventes precios, horarios, "
        "distancias ni hechos que no estén respaldados. Redacta un solo artículo útil "
        "en español, con tono cercano y claro. Devuelve exclusivamente JSON válido con "
        'las claves "title", "slug", "keywords", "excerpt", "category" y "body". '
        "keywords debe ser una lista de entre 3 y 8 frases SEO relevantes. "
        "El body debe ser texto plano con párrafos separados por saltos de línea, sin HTML."
    )
    user_prompt = (
        "Crea un artículo original sobre turismo en Santiago usando estas fuentes. "
        "No menciones que eres una IA ni describas el proceso de investigación.\n\n"
        f"FUENTES:\n{source_context}"
    )
    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
        "max_tokens": settings.deepseek_max_output_tokens,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(
            base_url=settings.deepseek_base_url.rstrip("/"),
            timeout=settings.deepseek_timeout_seconds,
        ) as client:
            response = await client.post("/chat/completions", headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise DeepSeekError(f"No se pudo conectar con DeepSeek: {exc}") from exc

    if response.status_code >= 400:
        detail = response.text[:500]
        raise DeepSeekError(f"DeepSeek respondió HTTP {response.status_code}: {detail}")

    try:
        result = response.json()
    except ValueError as exc:
        raise DeepSeekError("DeepSeek devolvió una respuesta no válida.") from exc
    return _extract_content(result), normalize_usage(result)


def pricing_snapshot() -> dict[str, Any]:
    hit, miss, output = _pricing()
    return {
        "mode": settings.deepseek_pricing_mode,
        "cache_hit_price_per_million": hit,
        "cache_miss_price_per_million": miss,
        "output_price_per_million": output,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
