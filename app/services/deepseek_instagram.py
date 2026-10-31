"""DeepSeek client for Instagram auto-replies with knowledge tool lookup."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.instagram_knowledge import DEEPSEEK_TOOLS, hostel_info_dict
from app.services.mcp_sources import call_reply_tool

SANTIAGO = ZoneInfo("America/Santiago")
REPLY_LOOP_TIMEOUT_SECONDS = 70.0
_RESERVATION_RE = re.compile(
    r"("
    r"reserv(?:a|ar|ame|arme|anos|arte|aste|astes|ó|o|amos|aron)?"
    r"|booking|book(?:ing|ed|\s*me)?"
    r"|hacer\s+una\s+reserva|quiero\s+reservar|necesito\s+reservar"
    r")",
    re.IGNORECASE,
)
_GREETING_ONLY_RE = re.compile(
    r"^\s*(?:[¡!]?\s*)?(?:"
    r"hola+|holi+|hello+|hi+|hey+|buenas|"
    r"buenos\s+d[ií]as|buen\s+d[ií]a|buenas\s+tardes|buenas\s+noches|"
    r"good\s+morning|good\s+afternoon|good\s+evening|"
    r"bom\s+dia|boa\s+tarde|boa\s+noite|ol[aá]+"
    r")(?:\s*[,!.…?]*)?(?:\s+(?:👋|🙂|😊|✨))?\s*$",
    re.IGNORECASE,
)


class DeepSeekConfigError(Exception):
    pass


class DeepSeekError(Exception):
    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.details = details


def deepseek_configured() -> bool:
    return bool((settings.deepseek_api_key or "").strip())


DEFAULT_SYSTEM_PROMPT = """Eres un especialista en atención hotelera Y vendedor de Black Cat Hostal Boutique
(Barrio Brasil, Santiago de Chile). Respondes comentarios y mensajes de Instagram y Facebook.

Estilo:
- Habla como una persona real de recepción: cercano, claro, natural. Nunca suenes a robot ni a script.
- Si el huésped solo saluda (“hola”, “hi”, “buenas”), responde CORTO y humano: saluda igual,
  pregunta en qué ayudas y PARA. No sueltes catálogo, beneficios ni pitch de venta en un saludo.
- Cuando SÍ haya una consulta (precios, habitaciones, ubicación, etc.), sé buen vendedor:
  dato + un beneficio real (Barrio Brasil, desayuno, ubicación, ambiente boutique) + cierre suave
  a WhatsApp si encaja. Solo hechos de las tools; no inventes ofertas.
- Evita plantillas (“Estimado huésped”, “Quedamos atentos”, listas largas). Máximo 1 emoji.
- Contesta SIEMPRE en el mismo idioma de la pregunta del huésped.
- Idioma (OBLIGATORIO): por defecto español. Nombres de lugares en inglés o mixtos
  (Costanera Center, Sky Costanera, Mall Plaza, Uber, etc.) NO cambian el idioma:
  si el resto del mensaje es español o solo es el nombre del lugar, responde en español.
  Nunca contestes en francés, alemán u otro idioma salvo que el huésped escriba claramente en ese idioma.
- Respuestas concisas para redes (máx. ~520 caracteres). Rutas hasta 800. Saludos: máx. ~120.

Datos (OBLIGATORIO):
- Precios, tipos o características de habitación → SOLO search_common_answers (preguntas frecuentes). Nunca uses habitaciones del panel ni search_rooms.
- Check-in, check-out, horario de desayuno, estacionamiento, WhatsApp, dirección → llama get_hostel_info.
- Otras dudas del hostal → llama search_common_answers y responde con el campo answer, en el idioma del huésped.
- Si la respuesta no está completa en las preguntas frecuentes ni en la info del hostal, llama search_web. Si necesitas el detalle de un resultado, llama read_web_page con esa URL. No uses la web para inventar precios de habitación.
- Cómo llegar desde el hostal a un lugar de Santiago → llama get_directions_from_hostel. Prioriza metro, micro/bus o auto; a pie solo si no hay metro/bus. Usa replies casi textual, una sola vez, sin repetir. Cierra con “Te lo indico en el siguiente mapa.” sin pegar ninguna URL (la imagen del mapa se envía aparte). No digas Google Maps. No reescribas la ruta ni mandes a WhatsApp.
- Cita SOLO hechos devueltos por las tools. Nunca inventes tarifas, tiempos ni calles.
- Los precios son referenciales en CLP; indica que pueden variar por temporada.
- Reserva / reservar / “reservame” / “me reservaste” / “quiero reservar” / “hazme una reserva”
  o cualquier pedido de reserva (con o sin fechas) → action=reply. Llama get_hostel_info,
  incluye el whatsapp_url real en el message y pide que escriban por WhatsApp con fechas
  y cantidad de personas. No confirmes ni hagas la reserva por Instagram/Facebook.
- Pago, factura, cancelación conflictiva o reclamo → action=escalate.
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


def _day_part(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 20:
        return "afternoon"
    return "night"


def current_greeting(language: str = "es") -> str:
    part = _day_part(datetime.now(SANTIAGO).hour)
    greetings = {
        "es": {"morning": "Buenos días", "afternoon": "Buenas tardes", "night": "Buenas noches"},
        "en": {"morning": "Good morning", "afternoon": "Good afternoon", "night": "Good evening"},
        "pt": {"morning": "Bom dia", "afternoon": "Boa tarde", "night": "Boa noite"},
    }
    return greetings.get(language, greetings["es"])[part]


_GREETING_OPENERS = (
    re.compile(
        r"^\s*(?:¡?\s*)?(?:"
        r"buenos\s+d[ií]as|buenas\s+tardes|buenas\s+noches|"
        r"hola(?:\s+de\s+nuevo)?|hello(?:\s+again)?|hi(?:\s+again)?|"
        r"good\s+morning|good\s+afternoon|good\s+(?:evening|night)|"
        r"bom\s+dia|boa\s+tarde|boa\s+noite"
        r")\s*[,!.…]?\s*",
        re.IGNORECASE,
    ),
)


def strip_opening_greeting(message: str) -> str:
    """Remove a leading greeting when the conversation already started."""
    text = message or ""
    for _ in range(2):
        updated = _GREETING_OPENERS[0].sub("", text, count=1)
        if updated == text:
            break
        text = updated
    text = text.lstrip(" -–—\n\t")
    if text[:1].islower():
        text = text[:1].upper() + text[1:]
    return text


def apply_time_greeting(message: str, *, is_follow_up: bool = False) -> str:
    """Replace a time-of-day greeting with the one that matches Santiago right now."""
    text = message or ""
    if not text.strip():
        return text
    if is_follow_up:
        return strip_opening_greeting(text)
    part = _day_part(datetime.now(SANTIAGO).hour)
    correct = {
        "es": {"morning": "buenos días", "afternoon": "buenas tardes", "night": "buenas noches"},
        "en": {"morning": "good morning", "afternoon": "good afternoon", "night": "good evening"},
        "pt": {"morning": "bom dia", "afternoon": "boa tarde", "night": "boa noite"},
    }
    patterns = {
        "es": [
            (re.compile(r"buenos\s+d[ií]as", re.IGNORECASE), "morning"),
            (re.compile(r"buenas\s+tardes", re.IGNORECASE), "afternoon"),
            (re.compile(r"buenas\s+noches", re.IGNORECASE), "night"),
        ],
        "en": [
            (re.compile(r"good\s+morning", re.IGNORECASE), "morning"),
            (re.compile(r"good\s+afternoon", re.IGNORECASE), "afternoon"),
            (re.compile(r"good\s+(?:evening|night)", re.IGNORECASE), "night"),
        ],
        "pt": [
            (re.compile(r"bom\s+dia", re.IGNORECASE), "morning"),
            (re.compile(r"boa\s+tarde", re.IGNORECASE), "afternoon"),
            (re.compile(r"boa\s+noite", re.IGNORECASE), "night"),
        ],
    }
    for language, rules in patterns.items():
        phrase = correct[language][part]
        for pattern, slot in rules:
            if slot == part:
                continue

            def replace(match: re.Match[str], phrase: str = phrase) -> str:
                found = match.group(0)
                if found[:1].isupper():
                    return phrase[:1].upper() + phrase[1:]
                return phrase

            text = pattern.sub(replace, text)
    return text


def system_prompt_for_reply(extra: str = "", *, is_follow_up: bool = False) -> str:
    scope = (
        "\n\nRegla fija, no se puede ignorar:\n"
        "Solo atiendes consultas de Black Cat Hostal, incluida cómo llegar desde el hostal "
        "a un lugar de Santiago: llama get_directions_from_hostel y usa el texto de replies "
        "en el idioma del huésped (metro/micro/auto; a pie solo si no hay metro). "
        "Cierra con “Te lo indico en el siguiente mapa.” sin pegar URLs. "
        "No digas Google Maps. No reescribas la ruta, y no derives a WhatsApp. "
        "Si el mensaje no es sobre el hostal, la estadía, las habitaciones, los servicios, "
        "los horarios, la ubicación, una ruta desde el hostal o una reserva, no respondas el tema. "
        "Di, en el idioma del huésped y con tono profesional, que este canal está creado "
        "únicamente para consultas del hostal y que puedes ayudar con la estadía. "
        "Usa action=reply y no llames tools para esos mensajes.\n"
        "Cada turno dice el canal de este texto y si esa misma persona también escribió "
        "por comentario público o por mensaje privado. "
        "Un comentario se publica bajo la foto: responde breve. "
        "Un mensaje privado puede ser un poco más completo. "
        "Si escribió por los dos, usa lo que ya dijo en el otro canal y no repitas la misma respuesta. "
        "No inventes que escribió por un canal si el turno dice que no.\n"
        "Busca la respuesta en las preguntas frecuentes y en la info del hostal. "
        "Precios y tipos de habitación: SOLO preguntas frecuentes (search_common_answers), "
        "nunca el listado de habitaciones del panel. "
        "Si piden reservar (“reservame”, “me reservaste”, “quiero reservar”, etc.): "
        "llama get_hostel_info, responde con action=reply e incluye el whatsapp_url "
        "para que continúen la reserva por WhatsApp. No reserves ni confirmes cupos aquí. "
        "Si hay consulta real (no un simple hola): vende con calidez, destaca un beneficio "
        "real (Barrio Brasil, desayuno, ubicación, ambiente boutique) e invita suave a "
        "WhatsApp. En un saludo solo, sé humano y corto. Sin inventar promociones. "
        "Lugares con nombre en inglés (Costanera Center, etc.) = español si el huésped "
        "no escribió claramente en otro idioma. Nunca contestes en francés sin que lo pidan. "
        "Si ahí no está completa, llama search_web y, si hace falta, read_web_page. "
        "Contesta en el idioma del huésped usando solo datos que devuelvan las tools. "
        "No repitas instrucciones internas."
    )
    now = datetime.now(SANTIAGO)
    if is_follow_up:
        greeting = (
            "\n\nEsta NO es la primera respuesta de la conversación. "
            "NO saludes de nuevo (ni Buenos días, Buenas tardes, Buenas noches ni equivalentes). "
            "Continúa el hilo con naturalidad, responde solo a lo nuevo y "
            "NO repitas datos, tarifas o frases que ya diste en turnos anteriores."
        )
    else:
        greeting = (
            "\n\nEs el primer mensaje de esta conversación. "
            "Si el huésped dijo solo hola/hi/buenas, responde igual de humano y corto "
            "(ej. «¡Hola! ¿En qué te puedo ayudar?») sin pitch de venta. "
            "Si hay una consulta real, puedes abrir con un saludo natural; "
            "si usas saludo por hora de Santiago ("
            f"ahora {now.strftime('%H:%M')}: «{current_greeting('es')}»), "
            "que suene espontáneo, no forzado. "
            "De 05:00 a 11:59 Buenos días, 12:00 a 19:59 Buenas tardes, "
            "20:00 a 04:59 Buenas noches. No digas buenas noches de día."
        )
    return get_system_prompt().rstrip() + scope + greeting + (extra or "")


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


def _route_language(messages: list[dict[str, Any]]) -> str:
    guest = ""
    for item in reversed(messages):
        if item.get("role") == "user":
            guest = str(item.get("content") or "")
            break
    # Prefer the raw guest line inside our wrapped turn, not the instruction tail.
    raw = guest
    marker = "Mensaje del huésped:\n"
    if marker in guest:
        raw = guest.split(marker, 1)[1]
        raw = raw.split("\n\n", 1)[0]
    sample = raw.lower().strip()
    # English place names alone (Costanera Center) must stay Spanish.
    if re.search(
        r"\b(how|where|walk|directions|from the hotel|from the hostel|please|can you)\b",
        sample,
    ):
        return "en"
    if re.search(r"\b(onde|chegar|caminho|você|voce|do hostel|por favor)\b", sample):
        return "pt"
    return "es"


def _prepared_route_reply(
    route: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    is_follow_up: bool = False,
) -> str:
    replies = route.get("replies") if isinstance(route.get("replies"), dict) else {}
    language = _route_language(messages)
    text = str(replies.get(language) or replies.get("es") or "").strip()
    if not text:
        return ""
    # Strip leftover Google branding / URLs if an old template slipped through.
    text = re.sub(
        r"(?im)^(?:ver en google maps|see on google maps|ver no google maps)\s*$",
        "",
        text,
    )
    text = re.sub(
        r"https?://(?:www\.)?(?:google\.com/maps|maps\.google\.com|goo\.gl/maps|maps\.app\.goo\.gl)\S*",
        "",
        text,
    )
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if is_follow_up:
        return strip_opening_greeting(text)
    if re.search(
        r"^\s*(buenos\s+d[ií]as|buenas\s+tardes|buenas\s+noches|good\s+morning|"
        r"good\s+afternoon|good\s+evening|bom\s+dia|boa\s+tarde|boa\s+noite|hola|hi[!]|ol[aá])\b",
        text,
        re.IGNORECASE,
    ):
        return text
    greeting = current_greeting(language)
    return f"{greeting}! {text}"


def _channel_label(item_type: str) -> str:
    if item_type == "comment":
        return "comentario público"
    if item_type in {"message", "dm"}:
        return "mensaje privado"
    return "desconocido"


def _history_lines(title: str, rows: list[dict[str, Any]]) -> list[str]:
    texts = [str(row.get("text") or "").strip() for row in rows if str(row.get("text") or "").strip()]
    if not texts:
        return []
    lines = [title]
    lines.extend(f"- {text}" for text in texts[:4])
    return lines


def _guest_turn(
    item_type: str,
    text: str,
    *,
    author_username: str | None = None,
    person_history: dict[str, Any] | None = None,
) -> str:
    history = person_history or {}
    comments = list(history.get("comments") or [])
    messages = list(history.get("messages") or [])
    wrote_comment = "sí" if comments or item_type == "comment" else "no"
    wrote_message = "sí" if messages or item_type in {"message", "dm"} else "no"
    lines = [
        f"Canal de este texto: {_channel_label(item_type)}.",
        f"Esta persona escribió por comentario: {wrote_comment}.",
        f"Esta persona escribió por mensaje privado: {wrote_message}.",
        *_history_lines("Otros comentarios de esta persona:", comments),
        *_history_lines("Otros mensajes privados de esta persona:", messages),
        f"Usuario: @{author_username or 'huesped'}",
        f"Mensaje del huésped:\n{(text or '').strip() or '(vacío)'}",
        "",
        "Detecta el idioma del mensaje y responde en ese mismo idioma. "
        "Si solo nombran un lugar de Santiago (ej. Costanera Center) o escriben en español "
        "con un nombre en inglés, responde en español y usa get_directions_from_hostel. "
        "Nunca respondas en francés u otro idioma no pedido. "
        "Busca en preguntas frecuentes e info del hostal; precios/tipos de habitación "
        "solo en preguntas frecuentes. "
        "Si solo saludó (hola/hi), responde humano y breve; no vendas ni listes beneficios. "
        "Si el mensaje es sobre reservar, llama get_hostel_info "
        "y manda al huésped al WhatsApp (whatsapp_url) para completar la reserva. "
        "Si hay una consulta real, responde como vendedor: dato + beneficio + cierre suave. "
        "Usa la web solo si falta el dato. "
        "Luego entrega el JSON final.",
    ]
    return "\n".join(lines)


def _looks_like_reservation(text: str) -> bool:
    sample = (text or "").strip()
    if not sample:
        return False
    return bool(_RESERVATION_RE.search(sample))


def _looks_like_greeting_only(text: str) -> bool:
    """True for one or several greeting bubbles sent in a burst (hola + buenas)."""
    sample = (text or "").strip()
    if not sample or len(sample) > 120:
        return False
    lines = [line.strip() for line in sample.splitlines() if line.strip()]
    if not lines:
        return False
    return all(bool(_GREETING_ONLY_RE.match(line)) for line in lines)


def _already_offered_help(prior: list[dict[str, Any]] | None) -> bool:
    for turn in reversed(prior or []):
        if turn.get("role") != "assistant":
            continue
        text = (turn.get("content") or "").strip().lower()
        return any(
            phrase in text
            for phrase in (
                "en qué te puedo ayudar",
                "en que te puedo ayudar",
                "how can i help",
                "em que posso",
            )
        )
    return False


def _human_greeting_reply(
    *,
    inbound_text: str,
    is_follow_up: bool,
    prior: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Short human hello — never dump a sales brochure on a bare greeting."""
    # People often send "Hola" then "Buenas" as separate bubbles: answer once only.
    if is_follow_up and _already_offered_help(prior):
        return {
            "action": "skip",
            "message": "",
            "reason": "duplicate_greeting",
            "model": "rule:greeting",
            "map_image_url": "",
            "maps_url": "",
        }
    sample = (inbound_text or "").strip().lower()
    # Use word boundaries — "ola" must NOT match inside Spanish "hola".
    if re.search(
        r"\b(hello|hi|hey|good\s+morning|good\s+afternoon|good\s+evening)\b",
        sample,
    ):
        message = "Hey! How can I help you with your stay at Black Cat?"
    elif re.search(r"\b(bom\s+dia|boa\s+tarde|boa\s+noite|ol[áa])\b", sample) and "hola" not in sample:
        message = "Oi! Em que posso te ajudar com a estadia no Black Cat?"
    else:
        # Default Spanish for hola / buenas / anything unclear.
        message = "¡Hola! ¿En qué te puedo ayudar?"
    return {
        "action": "reply",
        "message": message,
        "reason": "greeting_only",
        "model": "rule:greeting",
        "map_image_url": "",
        "maps_url": "",
    }


def _reservation_whatsapp_reply(
    db: Session,
    *,
    inbound_text: str,
    is_follow_up: bool,
) -> dict[str, Any]:
    """Fast path: never hang on DeepSeek when the guest asks to book."""
    info = hostel_info_dict(db)
    whatsapp = str(info.get("whatsapp_url") or "").strip() or "https://wa.me/56949105984"
    sample = (inbound_text or "").lower()
    if any(token in sample for token in ("please", "book", "booking", "reserve", "room")):
        message = (
            "Perfect — we'd love to host you in Barrio Brasil, steps from the center, "
            "with continental breakfast included. "
            f"Message us on WhatsApp {whatsapp} with your dates and guest count "
            "and we'll lock your room."
        )
    else:
        message = (
            "¡Claro! Te va a encantar quedarte en Barrio Brasil: ubicación céntrica, "
            "ambiente boutique y desayuno continental incluido. "
            f"Escríbenos por WhatsApp {whatsapp} con fechas y cantidad de personas "
            "y te armamos la reserva."
        )
    message = apply_time_greeting(message, is_follow_up=is_follow_up)
    return {
        "action": "reply",
        "message": message,
        "reason": "reservation_whatsapp",
        "model": "rule:reservation",
        "map_image_url": "",
        "maps_url": "",
    }


async def _run_reply_loop(
    *,
    db: Session,
    messages: list[dict[str, Any]],
    base: str,
    model: str,
    headers: dict[str, str],
    is_follow_up: bool = False,
) -> dict[str, Any]:
    route_reply: dict[str, Any] | None = None

    async with httpx.AsyncClient(timeout=45.0) as client:
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
                    try:
                        result = await asyncio.wait_for(
                            call_reply_tool(name, args),
                            timeout=35.0,
                        )
                    except asyncio.TimeoutError:
                        result = {
                            "ok": False,
                            "error": f"La tool {name} tardó demasiado.",
                        }
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
            map_image_url = ""
            maps_url = ""
            if action == "reply" and route_reply:
                prepared = _prepared_route_reply(
                    route_reply,
                    messages,
                    is_follow_up=is_follow_up,
                )
                if prepared:
                    reply_message = prepared
                map_image_url = str(route_reply.get("map_image_url") or "").strip()
                maps_url = str(route_reply.get("maps_url") or "").strip()
            reply_message = apply_time_greeting(reply_message, is_follow_up=is_follow_up)
            # Never leave a raw Google Maps /maps/dir URL in the guest text (IG puts preview on top).
            reply_message = re.sub(
                r"https?://(?:www\.)?(?:google\.com/maps|maps\.googleapis\.com|maps\.google\.com|goo\.gl/maps|maps\.app\.goo\.gl)\S*",
                "",
                reply_message or "",
            )
            reply_message = re.sub(r"\n{3,}", "\n\n", reply_message).strip()
            if action == "reply" and not reply_message:
                action = "escalate"
                reason = reason or "empty_reply"
            return {
                "action": action,
                "message": reply_message,
                "reason": reason,
                "model": model,
                "map_image_url": map_image_url,
                "maps_url": maps_url,
            }

    raise DeepSeekError("DeepSeek agotó el límite de tool calls sin respuesta final")


def _assistant_turn(text: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": json.dumps(
            {"action": "reply", "message": text, "reason": "turno_previo"},
            ensure_ascii=False,
        ),
    }


async def generate_instagram_reply(
    *,
    db: Session,
    inbound_text: str,
    item_type: str,
    author_username: str | None = None,
    person_history: dict[str, Any] | None = None,
    thread_history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Return {action, message, reason, model} using DeepSeek + knowledge tools."""
    prior = [
        turn
        for turn in (thread_history or [])
        if (turn.get("content") or "").strip() and turn.get("role") in {"user", "assistant"}
    ]
    is_follow_up = bool(prior)
    if _looks_like_greeting_only(inbound_text):
        return _human_greeting_reply(
            inbound_text=inbound_text,
            is_follow_up=is_follow_up,
            prior=prior,
        )
    if _looks_like_reservation(inbound_text):
        return _reservation_whatsapp_reply(
            db,
            inbound_text=inbound_text,
            is_follow_up=is_follow_up,
        )
    base, model, headers = _require_deepseek()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt_for_reply(is_follow_up=is_follow_up)},
    ]
    for turn in prior[-12:]:
        text = (turn.get("content") or "").strip()
        if turn.get("role") == "user":
            messages.append(
                {
                    "role": "user",
                    "content": _guest_turn(
                        item_type,
                        text,
                        author_username=author_username,
                        person_history=person_history,
                    ),
                }
            )
        else:
            messages.append(_assistant_turn(text))
    messages.append(
        {
            "role": "user",
            "content": _guest_turn(
                item_type,
                inbound_text,
                author_username=author_username,
                person_history=person_history,
            ),
        }
    )
    if is_follow_up:
        messages[-1]["content"] += (
            "\n\nEsto es una continuación del hilo. No saludes otra vez y no repitas "
            "la información que ya diste arriba; responde solo a este mensaje nuevo."
        )
    try:
        return await asyncio.wait_for(
            _run_reply_loop(
                db=db,
                messages=messages,
                base=base,
                model=model,
                headers=headers,
                is_follow_up=is_follow_up,
            ),
            timeout=REPLY_LOOP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise DeepSeekError(
            "DeepSeek tardó demasiado y se canceló para no dejar el inbox pegado"
        ) from exc


async def generate_test_chat(
    *,
    db: Session,
    channel: str,
    history: list[dict[str, str]],
) -> dict[str, Any]:
    """Same reply pipeline as Instagram/Facebook, with prior turns for testing."""
    item_type = "dm" if channel == "instagram" else "comment"
    channel_label = "Instagram" if channel == "instagram" else "Facebook"
    turns = [turn for turn in history if (turn.get("content") or "").strip()]
    if not turns or turns[-1].get("role") != "user":
        raise DeepSeekError("El último mensaje debe ser del huésped")
    prior = turns[:-1]
    is_follow_up = bool(prior)
    inbound_text = turns[-1]["content"]
    if _looks_like_greeting_only(inbound_text):
        result = _human_greeting_reply(
            inbound_text=inbound_text,
            is_follow_up=is_follow_up,
            prior=prior,
        )
        result["channel"] = channel
        return result
    if _looks_like_reservation(inbound_text):
        result = _reservation_whatsapp_reply(
            db,
            inbound_text=inbound_text,
            is_follow_up=is_follow_up,
        )
        result["channel"] = channel
        return result
    base, model, headers = _require_deepseek()
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": system_prompt_for_reply(
                f"\n\nCanal de esta conversación de prueba: {channel_label}.",
                is_follow_up=is_follow_up,
            ),
        }
    ]

    for turn in prior:
        role = turn.get("role")
        text = (turn.get("content") or "").strip()
        if role == "user":
            messages.append({"role": "user", "content": _guest_turn(item_type, text)})
        elif role == "assistant":
            messages.append(_assistant_turn(text))
    messages.append({"role": "user", "content": _guest_turn(item_type, inbound_text)})
    if is_follow_up:
        messages[-1]["content"] += (
            "\n\nEsto es una continuación del hilo. No saludes otra vez y no repitas "
            "la información que ya diste arriba; responde solo a este mensaje nuevo."
        )
    try:
        result = await asyncio.wait_for(
            _run_reply_loop(
                db=db,
                messages=messages,
                base=base,
                model=model,
                headers=headers,
                is_follow_up=is_follow_up,
            ),
            timeout=REPLY_LOOP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise DeepSeekError(
            "DeepSeek tardó demasiado y se canceló para no dejar el chat pegado"
        ) from exc
    result["channel"] = channel
    return result
