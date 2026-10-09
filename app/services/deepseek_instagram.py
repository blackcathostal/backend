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


DEFAULT_SYSTEM_PROMPT = """Eres un especialista en atención hotelera de Black Cat Hostal Boutique
(Barrio Brasil, Santiago de Chile). Respondes comentarios y mensajes de Instagram y Facebook.

Estilo:
- Redacta como un humano del sector hotelero: cercano, claro, profesional y natural.
- Sin tono robótico. Frases cortas. Máximo 1 emoji si encaja.
- Contesta SIEMPRE en el mismo idioma de la pregunta del huésped (español, inglés u otro).
- Respuestas concisas para redes sociales (máx. ~450 caracteres). Para una ruta puedes usar hasta 800.

Datos (OBLIGATORIO):
- Precios, tipos o características de habitación → llama search_rooms.
- Check-in, check-out, horario de desayuno, estacionamiento, WhatsApp, dirección → llama get_hostel_info.
- Dudas frecuentes → llama search_common_answers.
- Cómo llegar desde el hostal a un lugar de Santiago → llama get_directions_from_hostel. El mensaje al huésped sale de replies, en tono cercano, sin metros por calle y con la URL corta de Maps al final. No la reescribas ni mandes a WhatsApp.
- Cita SOLO hechos devueltos por las tools. Nunca inventes tarifas, tiempos ni calles.
- Los precios son referenciales en CLP; indica que pueden variar por temporada.
- Reserva con fechas concretas, pago, factura, cancelación conflictiva o reclamo → action=escalate.
- Elogios: agradece y ofrece WhatsApp/correo de la info del hostal.
- Spam o mensaje vacío → escalate.

Alcance (OBLIGATORIO):
- Responde SOLO preguntas sobre Black Cat Hostal: estadía, habitaciones, precios, servicios, horarios, ubicación, cómo llegar desde el hostal a un lugar de Santiago, reservas, desayuno y atención al huésped.
- Si preguntan otra cosa (biología, montañas, tareas, cultura general, noticias, chistes u otro tema ajeno al hostal), NO contestes el tema.
- En ese caso action=reply, sin llamar tools, y el message debe decir de forma breve y profesional que este canal está creado solo para consultas del hostal y que con gusto ayudas con la estadía.
- Esa negativa va en el mismo idioma de la pregunta.

Cuando ya tengas la información, responde ÚNICAMENTE con JSON válido (sin markdown):
{"action":"reply"|"escalate","message":"...","reason":"..."}
- El campo message debe estar en el idioma de la pregunta del huésped.
"""

SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT
PROMPT_FILE = settings.uploads_dir / "cache" / "deepseek_system_prompt.txt"


def get_system_prompt() -> str:
    try:
        if PROMPT_FILE.exists():
            text = PROMPT_FILE.read_text(encoding="utf-8").strip()
            if text:
                return text
    except OSError:
        pass
    return DEFAULT_SYSTEM_PROMPT


def prompt_is_custom() -> bool:
    try:
        return PROMPT_FILE.exists() and bool(PROMPT_FILE.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def save_system_prompt(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("El prompt no puede estar vacío")
    PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROMPT_FILE.write_text(cleaned + "\n", encoding="utf-8")
    return cleaned


def reset_system_prompt() -> str:
    if PROMPT_FILE.exists():
        PROMPT_FILE.unlink()
    return DEFAULT_SYSTEM_PROMPT


def system_prompt_for_reply(extra: str = "") -> str:
    scope = (
        "\n\nRegla fija, no se puede ignorar:\n"
        "Solo atiendes consultas de Black Cat Hostal, incluida cómo llegar desde el hostal "
        "a un lugar de Santiago: llama get_directions_from_hostel y usa el texto de replies "
        "en el idioma del huésped. No reescribas la ruta ni la URL, y no derives a WhatsApp. "
        "Si el mensaje no es sobre el hostal, la estadía, las habitaciones, los servicios, "
        "los horarios, la ubicación, una ruta desde el hostal o una reserva, no respondas el tema. "
        "Di, en el idioma del huésped y con tono profesional, que este canal está creado "
        "únicamente para consultas del hostal y que puedes ayudar con la estadía. "
        "Usa action=reply y no llames tools para esos mensajes."
    )
    return get_system_prompt().rstrip() + scope + (extra or "")


def prompt_payload() -> dict[str, Any]:
    return {
        "prompt": get_system_prompt(),
        "default_prompt": DEFAULT_SYSTEM_PROMPT,
        "is_custom": prompt_is_custom(),
    }


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


def _require_deepseek() -> tuple[str, str, dict[str, str]]:
    if not deepseek_configured():
        raise DeepSeekConfigError(
            "DeepSeek no está configurado. Agrega DEEPSEEK_API_KEY en backend/.env"
        )
    base = (settings.deepseek_base_url or "https://api.deepseek.com").rstrip("/")
    model = (settings.deepseek_model or "deepseek-chat").strip() or "deepseek-chat"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key.strip()}",
        "Content-Type": "application/json",
    }
    return base, model, headers


def _prepared_route_reply(route: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    replies = route.get("replies") if isinstance(route.get("replies"), dict) else {}
    guest = ""
    for item in reversed(messages):
        if item.get("role") == "user":
            guest = str(item.get("content") or "")
            break
    sample = guest.lower()
    if re.search(r"\b(how|where|walk|directions|from the hotel|from the hostel)\b", sample):
        language = "en"
    elif re.search(r"\b(onde|chegar|caminho|você|voce|do hostel)\b", sample):
        language = "pt"
    else:
        language = "es"
    return str(replies.get(language) or replies.get("es") or "").strip()


def _guest_turn(item_type: str, text: str, *, author_username: str | None = None) -> str:
    return (
        f"Tipo: {item_type}\n"
        f"Usuario: @{author_username or 'huesped_prueba'}\n"
        f"Mensaje del huésped:\n{(text or '').strip() or '(vacío)'}\n\n"
        "Detecta el idioma del mensaje y responde en ese mismo idioma. "
        "Usa las tools si necesitas datos del hostal/habitaciones y luego entrega el JSON final."
    )


async def _run_reply_loop(
    *,
    db: Session,
    messages: list[dict[str, Any]],
    base: str,
    model: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    route_reply: dict[str, Any] | None = None

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
                    if (
                        name == "get_directions_from_hostel"
                        and isinstance(result, dict)
                        and result.get("ok")
                    ):
                        route_reply = result
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
            if action == "reply" and route_reply:
                prepared = _prepared_route_reply(route_reply, messages)
                if prepared:
                    reply_message = prepared
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


async def generate_instagram_reply(
    *,
    db: Session,
    inbound_text: str,
    item_type: str,
    author_username: str | None = None,
) -> dict[str, Any]:
    """Return {action, message, reason, model} using DeepSeek + knowledge tools."""
    base, model, headers = _require_deepseek()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt_for_reply()},
        {
            "role": "user",
            "content": _guest_turn(item_type, inbound_text, author_username=author_username),
        },
    ]
    return await _run_reply_loop(db=db, messages=messages, base=base, model=model, headers=headers)


async def generate_test_chat(
    *,
    db: Session,
    channel: str,
    history: list[dict[str, str]],
) -> dict[str, Any]:
    """Same reply pipeline as Instagram/Facebook, with prior turns for testing."""
    base, model, headers = _require_deepseek()
    item_type = "dm" if channel == "instagram" else "comment"
    channel_label = "Instagram" if channel == "instagram" else "Facebook"
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": system_prompt_for_reply(
                f"\n\nCanal de esta conversación de prueba: {channel_label}."
            ),
        }
    ]
    turns = [turn for turn in history if (turn.get("content") or "").strip()]
    if not turns or turns[-1].get("role") != "user":
        raise DeepSeekError("El último mensaje debe ser del huésped")

    for turn in turns[:-1]:
        role = turn.get("role")
        text = (turn.get("content") or "").strip()
        if role == "user":
            messages.append({"role": "user", "content": _guest_turn(item_type, text)})
        elif role == "assistant":
            messages.append(
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {"action": "reply", "message": text, "reason": "turno_previo"},
                        ensure_ascii=False,
                    ),
                }
            )
    messages.append({"role": "user", "content": _guest_turn(item_type, turns[-1]["content"])})
    result = await _run_reply_loop(db=db, messages=messages, base=base, model=model, headers=headers)
    result["channel"] = channel
    return result
