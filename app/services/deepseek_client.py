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
        "api_requests": 1,
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


async def generate_article(
    source_context: str,
    *,
    avoid_articles: list[str] | None = None,
    revision_note: str = "",
    editorial_direction: str = "",
) -> tuple[str, dict[str, Any]]:
    if not settings.deepseek_api_key.strip():
        raise DeepSeekError("DEEPSEEK_API_KEY no está configurada en el backend.")

    system_prompt = (
        "Eres un guía de turismo profesional especializado en Santiago de Chile. "
        "Redacta un artículo de blog turístico detallista, práctico y completo, "
        "pero condensado y entretenido para una persona: elimina relleno, repeticiones "
        "y frases genéricas. Usa únicamente los datos incluidos en FUENTES, DATOS "
        "VERIFICADOS DE GOOGLE MAPS y APIs REST. No inventes "
        "precios, horarios, distancias, direcciones, nombres ni recomendaciones. Si un "
        "dato no está en las fuentes, omítelo. Explica el contexto del lugar, qué puede "
        "hacer el visitante y cómo planificar la visita solo cuando las fuentes lo permitan. "
        "Cada afirmación debe poder verificarse directamente en las fuentes; no uses memoria "
        "ni conocimiento general para completar vacíos. Cuando hables de eventos culturales, "
        "conciertos, exposiciones o actividades, usa únicamente eventos que aparezcan en "
        "las APIs REST con fecha futura, lugar y enlace o fuente verificable. No presentes "
        "catálogos, lugares turísticos o datos meteorológicos como si fueran eventos. Si una "
        "API no está configurada o no devuelve eventos, no inventes una agenda alternativa. "
        "Los nombres, direcciones, enlaces, calificaciones y horarios de lugares deben "
        "salir de DATOS VERIFICADOS DE GOOGLE MAPS. Si Google Maps no entrega un horario "
        "o un dato, no lo completes ni lo supongas. "
        "Escribe como un redactor nativo de turismo: lenguaje natural, preciso y fluido, "
        "con transiciones variadas y sin sonar a plantilla de IA. Cada párrafo debe aportar "
        "un dato o recomendación distinta; no repitas el nombre de Santiago, la cordillera "
        "o el mismo atractivo para llenar espacio. "
        "Debe ser detallado sin volverse pesado: entre "
        f"500 y {settings.deepseek_article_max_words} palabras, en 6 a 8 párrafos breves. "
        "Desarrolla el tema con suficiente profundidad: explica el contexto del lugar o "
        "experiencia, qué verá el visitante, qué puede hacer allí y los datos prácticos "
        "que estén documentados. Ordena la información de forma progresiva, separa las "
        "ideas en subtítulos Markdown cuando ayude a leer y evita acumular datos sin "
        "explicarlos. La extensión no debe lograrse repitiendo ideas ni adjetivos. "
        "El título debe tener máximo 70 caracteres y el extracto entre 140 y 200 caracteres. "
        "Devuelve exclusivamente JSON válido con "
        'las claves "title", "slug", "keywords", "excerpt", "category", "body" y '
        '"place_queries". '
        "keywords debe ser una lista de entre 3 y 8 frases SEO relevantes. "
        "place_queries debe ser una lista de 1 a 4 búsquedas concretas para Google Maps, "
        "relacionadas con los lugares, restaurantes, cafés, talleres o servicios que "
        "realmente quieras mencionar. El body debe usar Markdown sencillo, sin HTML: "
        "párrafos separados por líneas en blanco, **negrita** para nombres, direcciones, "
        "horarios y datos clave, y listas con guiones cuando haya varios lugares o pasos. "
        "Cada nuevo artículo debe tener un tema, título, estructura, enfoque y vocabulario "
        "claramente diferentes de los artículos ya publicados. Antes de escribir, identifica "
        "el enfoque principal de los artículos anteriores y elige otro lugar, experiencia "
        "o ángulo. No uses la misma introducción, orden de ideas, ejemplos, consejos, "
        "conclusión ni frases características, aunque cambies algunas palabras. "
        "No vuelvas a convertir en tema principal un atractivo, cerro, barrio, museo, "
        "plaza, parque, mirador, ruta o restaurante ya tratado. Puedes mencionarlo "
        "brevemente como contexto si es imprescindible, pero el artículo debe aportar "
        "un enfoque y contenido nuevos. Si las fuentes no permiten un tema nuevo, no "
        "fuerces la redacción ni inventes datos. "
        "No comiences con fórmulas repetidas como 'Santiago, la capital chilena...' ni "
        "termines con frases vacías como 'una experiencia inolvidable' o 'un destino "
        "imperdible'. Si el título promete una cantidad de rutas, días o lugares, el "
        "cuerpo debe cumplir exactamente esa promesa. No agregues párrafos obligatorios "
        "sobre transporte, clima o alojamiento si no son necesarios para el tema."
    )
    user_prompt = (
        "Crea un artículo original sobre turismo en Santiago usando estas fuentes y APIs REST. "
        "No menciones que eres una IA ni describas el proceso de investigación. "
        "No repitas ningún artículo, título, introducción, conclusión ni lista de consejos "
        "anterior. Escribe con verbos concretos, escenas observables y detalles útiles, "
        "alterna la longitud de "
        "las frases y evita adjetivos promocionales o clichés turísticos. Antes de devolver "
        "el JSON, revisa silenciosamente que cada dato esté respaldado por FUENTES, que el "
        "artículo sea realmente diferente de los anteriores y que no tenga relleno. "
        "No repitas la fórmula 'Santiago es una metrópolis vibrante', 'a los pies de la "
        "Cordillera de los Andes' ni cierres como 'una experiencia inolvidable'.\n\n"
        f"FUENTES, APIS REST Y DATOS VERIFICADOS:\n{source_context}"
    )
    if editorial_direction:
        user_prompt += (
            "\n\nDIRECCIÓN EDITORIAL DE ESTA REDACCIÓN:\n"
            f"Explora preferentemente {editorial_direction}. Úsala solo si las fuentes "
            "la respaldan; si no, elige otro ángulo concreto y documentado. No copies "
            "la apertura ni la estructura de los artículos anteriores."
        )
    if avoid_articles:
        user_prompt += (
            "\n\nARTÍCULOS YA PUBLICADOS (úsalos solo para evitar coincidencias; no los "
            "resumas ni los imites):\n"
            + "\n".join(f"- {article}" for article in avoid_articles[:20])
        )
    if revision_note:
        user_prompt += f"\n\nINSTRUCCIÓN DE REVISIÓN:\n{revision_note}"
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
