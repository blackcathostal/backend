"""DeepSeek client for Instagram auto-replies with knowledge tool lookup."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.instagram_knowledge import DEEPSEEK_TOOLS, run_knowledge_tool


class DeepSeekConfigError(Exception):
    pass


class DeepSeekError(Exception):
    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.details = details


def deepseek_configured() -> bool:
    return bool((settings.deepseek_api_key or "").strip())


SYSTEM_PROMPT = """Eres un especialista en atención hotelera de Black Cat Hostal Boutique
(Barrio Brasil, Santiago de Chile). Respondes comentarios y mensajes directos de Instagram.

Estilo:
- Redacta como un humano del sector hotelero: cercano, claro, profesional y natural.
- Sin tono robótico. Frases cortas. Máximo 1 emoji si encaja.
- Contesta SIEMPRE en el mismo idioma de la pregunta del huésped (español, inglés u otro).
- Respuestas concisas para Instagram (máx. ~450 caracteres).

Datos (OBLIGATORIO):
- Precios, tipos o características de habitación → llama search_rooms.
- Check-in, check-out, horario de desayuno, estacionamiento, WhatsApp, dirección → llama get_hostel_info.
- Dudas frecuentes → llama search_common_answers.
- Cita SOLO hechos devueltos por las tools. Nunca inventes tarifas ni políticas.
- Los precios son referenciales en CLP; indica que pueden variar por temporada.
- Reserva con fechas concretas, pago, factura, cancelación conflictiva o reclamo → action=escalate.
- Elogios: agradece y ofrece WhatsApp/correo de la info del hostal.
- Spam o mensaje vacío → escalate.

Cuando ya tengas la información, responde ÚNICAMENTE con JSON válido (sin markdown):
{"action":"reply"|"escalate","message":"...","reason":"..."}
- El campo message debe estar en el idioma de la pregunta del huésped.
"""


def _extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise DeepSeekError("Respuesta vacía de DeepSeek")
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        data = json.loads(match.group(0))
        if isinstance(data, dict):
            return data
    raise DeepSeekError("DeepSeek no devolvió JSON válido", details=raw[:500])


def _parse_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def generate_instagram_reply(
    *,
    db: Session,
    inbound_text: str,
    item_type: str,
    author_username: str | None = None,
) -> dict[str, Any]:
    """Return {action, message, reason, model} using DeepSeek + knowledge tools."""
    if not deepseek_configured():
        raise DeepSeekConfigError(
            "DeepSeek no está configurado. Agrega DEEPSEEK_API_KEY en backend/.env"
        )

    base = (settings.deepseek_base_url or "https://api.deepseek.com").rstrip("/")
    model = (settings.deepseek_model or "deepseek-chat").strip() or "deepseek-chat"
    user_content = (
        f"Tipo: {item_type}\n"
        f"Usuario Instagram: @{author_username or 'desconocido'}\n"
        f"Mensaje del huésped:\n{(inbound_text or '').strip() or '(vacío)'}\n\n"
        "Detecta el idioma del mensaje y responde en ese mismo idioma. "
        "Usa las tools si necesitas datos del hostal/habitaciones y luego entrega el JSON final."
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key.strip()}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=90.0) as client:
        for _ in range(4):
            response = await client.post(
                f"{base}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "temperature": 0.45,
                    "tools": DEEPSEEK_TOOLS,
                    "tool_choice": "auto",
                    "messages": messages,
                },
            )
            if response.status_code >= 400:
                raise DeepSeekError(
                    f"DeepSeek error HTTP {response.status_code}",
                    details=response.text[:800],
                )

            payload = response.json()
            try:
                message = payload["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise DeepSeekError("Formato inesperado de DeepSeek", details=payload) from exc

            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content"),
                        "tool_calls": tool_calls,
                    }
                )
                for call in tool_calls:
                    fn = call.get("function") or {}
                    name = fn.get("name") or ""
                    args = _parse_tool_args(fn.get("arguments"))
                    result = run_knowledge_tool(db, name, args)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id") or name,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                continue

            content = (message.get("content") or "").strip()
            data = _extract_json(content)
            action = str(data.get("action") or "").strip().lower()
            if action not in {"reply", "escalate"}:
                action = "escalate"
            reply_message = str(data.get("message") or "").strip()
            reason = str(data.get("reason") or "").strip()
            if action == "reply" and not reply_message:
                action = "escalate"
                reason = reason or "empty_reply"
            return {
                "action": action,
                "message": reply_message,
                "reason": reason,
                "model": model,
            }

    raise DeepSeekError("DeepSeek agotó el límite de tool calls sin respuesta final")
