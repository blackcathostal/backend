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
_GUEST_GOODBYE_RE = re.compile(
    r"\b("
    r"chao|chau|adios|adi[oó]s|bye|goodbye|good\s*bye|hasta\s+luego|"
    r"nos\s+vemos|cu[ií]date|thanks?\s+bye|gracias\s*,?\s*chao|"
    r"tchau|até\s+logo|au\s+revoir"
    r")\b",
    re.IGNORECASE,
)
_BOT_FAREWELL_RE = re.compile(
    r"(?is)(?:^|[.!?…]\s+)"
    r"(?:"
    r"que\s+tengas\s+(?:un\s+)?buen\s+(?:d[ií]a|viaje|finde|fin\s+de\s+semana)|"
    r"que\s+esté?s?\s+bien|"
    r"cualquier\s+cosa\s+(?:me\s+)?escri(?:bes|ba)|"
    r"quedamos\s+atentos?|"
    r"saludos(?:\s+cordiales)?|"
    r"hasta\s+luego|hasta\s+pronto|nos\s+vemos|"
    r"chao|chau|adi[oó]s|"
    r"have\s+a\s+(?:great|nice|good)\s+(?:day|trip|one)|"
    r"take\s+care|best\s+regards|kind\s+regards|"
    r"at[eé]\s+logo|tchau"
    r")[^.!?…]*[.!?…]?\s*$"
)
_WA_ME_RE = re.compile(
    r"https?://(?:wa\.me/|api\.whatsapp\.com/send\?phone=)(\d{8,15})\S*",
    re.IGNORECASE,
)
_WA_ME_PLAIN_RE = re.compile(r"\bwa\.me/(\d{8,15})\b", re.IGNORECASE)


class DeepSeekConfigError(Exception):
    pass


class DeepSeekError(Exception):
    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.details = details


def deepseek_configured() -> bool:
    return bool((settings.deepseek_api_key or "").strip())


DEFAULT_SYSTEM_PROMPT = """Eres recepción de Black Cat Hostal Boutique (Santiago). Atiendes Instagram y Facebook
(Messenger). Mismas reglas en ambos canales.

Estilo (OBLIGATORIO):
- Respuestas EJECUTIVAS y cortas: 1–2 frases. Máx. ~220 caracteres (rutas hasta 450). Saludos ~60.
- Habla natural, sin robot ni script. Máximo 1 emoji y solo si encaja.
- NO repitas siempre lo mismo: no menciones Barrio Brasil, desayuno continental ni “ambiente boutique”
  en cada mensaje. Úsalos SOLO si el huésped pregunta por ubicación, barrio o desayuno.
- NUNCA te despidas (chao, hasta luego, que tengas buen día/viaje, saludos, quedamos atentos)
  a menos que el huésped se haya despedido primero en este mensaje.
- Si solo saluda (“hola”, “hi”, “buenas”) sin otra consulta: responde exactamente en ese idioma
  el equivalente de «Hola, ¿en qué te podemos ayudar?» y PARA. Sin catálogo ni pitch.
- Consulta real: contesta el dato pedido y listo. Sin relleno. Sin vender de más.
- No asumas aeropuerto/transporte ni de dónde viene el huésped si no lo dijo.

Idioma (OBLIGATORIO):
- Detecta el idioma por toda la charla y responde SOLO en ese idioma.
- Mensajes cortos (“sí”, “ok”) heredan el idioma del hilo. No mezcles idiomas.

Datos (OBLIGATORIO):
- Precios / tipos de habitación → SOLO search_common_answers. Nunca search_rooms ni panel.
- Check-in, check-out, desayuno, estacionamiento, WhatsApp, dirección → get_hostel_info.
- Otras dudas del hostal → search_common_answers.
- Si NO sabes o no está en FAQ/info: action=reply y deriva a WhatsApp con el número
  (formato +56 9 …, SIN pegar https://wa.me/… para evitar preview de imagen). Breve.
- Cómo llegar desde el hostal → get_directions_from_hostel. Una sola vez. Sin URLs.
  Cierra con “Te lo indico en el siguiente mapa.” No digas Google Maps. No inventes calles.
- Reserva → deriva a WhatsApp corto (fechas + personas). Nunca inventes estado de reserva.
- Tarifas: Matrimonial/Suite $54.990, Twin $54.990, Triple $69.990, cama $19.990.
- Pago/factura/reclamo conflictivo → escalate. Spam/vacío → escalate.

Alcance:
- Solo Black Cat Hostal. Tema ajeno: di breve que este canal es solo para el hostal.

JSON final (sin markdown):
{"action":"reply"|"escalate","message":"...","reason":"..."}
"""

SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT
PROMPT_FILE = settings.uploads_dir / "cache" / "deepseek_system_prompt.txt"


def get_system_prompt() -> str:
    try:
        if PROMPT_FILE.exists():
            text = PROMPT_FILE.read_text(encoding="utf-8").strip()
            # Ignore outdated custom prompts that still push long sales copy.
            if text and "EJECUTIVAS" in text and "NUNCA te despidas" in text:
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


def whatsapp_phone_display(whatsapp_url: str = "") -> str:
    """Human phone for DMs — avoids Instagram link-preview image before the text."""
    raw = (whatsapp_url or "").strip()
    digits = ""
    match = _WA_ME_RE.search(raw) or _WA_ME_PLAIN_RE.search(raw)
    if match:
        digits = match.group(1)
    else:
        digits = re.sub(r"\D", "", raw)
    if digits.startswith("56") and len(digits) >= 11:
        rest = digits[2:]
        if rest.startswith("9") and len(rest) == 9:
            return f"+56 9 {rest[1:5]} {rest[5:]}"
        return f"+56 {rest}"
    if digits:
        return f"+{digits}"
    return "+56 9 4910 5984"


def sanitize_outbound_message(message: str) -> str:
    """Replace wa.me links with a phone number so IG does not show WhatsApp preview first."""
    text = message or ""

    def _replace(match: re.Match[str]) -> str:
        return whatsapp_phone_display(match.group(0))

    text = _WA_ME_RE.sub(_replace, text)
    text = _WA_ME_PLAIN_RE.sub(_replace, text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def guest_said_goodbye(text: str) -> bool:
    return bool(_GUEST_GOODBYE_RE.search(text or ""))


def strip_unsolicited_goodbye(message: str, *, guest_text: str = "") -> str:
    if guest_said_goodbye(guest_text):
        return (message or "").strip()
    text = (message or "").strip()
    for _ in range(3):
        updated = _BOT_FAREWELL_RE.sub("", text).strip()
        if updated == text:
            break
        text = updated
    return text.rstrip(" ,;-")


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
        "\n\nReglas fijas (no se pueden ignorar):\n"
        "- Respuestas cortas y ejecutivas (1–2 frases). Sin párrafos ni listas largas.\n"
        "- Nunca te despidas si el huésped no se despidió primero.\n"
        "- No repitas Barrio Brasil / desayuno / boutique en cada mensaje; solo si preguntan eso.\n"
        "- Si no sabes la respuesta: deriva a WhatsApp con número +56 9… SIN link https://wa.me "
        "(el link genera preview/imagen antes del texto en Instagram).\n"
        "- Reserva: WhatsApp corto con fechas y personas. No inventes estado de reserva.\n"
        "- Precios solo FAQ: Matrimonial/Suite 54990, Twin 54990, Triple 69990, cama 19990.\n"
        "- Rutas: get_directions_from_hostel, sin URLs, «Te lo indico en el siguiente mapa.»\n"
        "- Tema ajeno al hostal: di breve que este canal es solo para el hostal.\n"
        "- Instagram y Facebook: mismas reglas.\n"
    )
    if is_follow_up:
        greeting = (
            "\nEsta NO es la primera respuesta. NO saludes de nuevo. "
            "Responde solo a lo nuevo. NO repitas datos ni frases ya dichas."
        )
    else:
        greeting = (
            "\nPrimer mensaje. Si solo dijo hola/hi/buenas: "
            "«Hola, ¿en qué te podemos ayudar?» (o equivalente en su idioma) y PARA. "
            "Si hay consulta real, contesta el dato sin pitch."
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


def _guest_raw_text(content: str) -> str:
    guest = str(content or "")
    marker = "Mensaje del huésped:\n"
    if marker in guest:
        guest = guest.split(marker, 1)[1]
        guest = guest.split("\n\n", 1)[0]
    return guest.strip()


_SHORT_AFFIRMATION_RE = re.compile(
    r"(?is)^\s*("
    r"sí|si|dale|ok|okay|claro|por\s+favor|si\s+por\s+favor|sí\s+por\s+favor|"
    r"yes|please|yes\s+please|yep|yeah|"
    r"sim|isso|pode|"
    r"oui|d'accord|"
    r"ja|genau"
    r")\s*[!?.…]*\s*$"
)

_LANG_SIGNAL_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    (
        "en",
        re.compile(
            r"\b(how|where|what|when|which|please|thanks|thank you|hello|hi|hey|"
            r"room|rooms|price|prices|available|booking|book|airport|directions|"
            r"from the hotel|from the hostel|can you|i want|i need|good morning|"
            r"good afternoon|good evening)\b",
            re.I,
        ),
        2,
    ),
    (
        "pt",
        re.compile(
            r"\b(onde|chegar|caminho|você|voce|obrigad[oa]|tudo bem|quarto|quartos|"
            r"preço|preco|disponível|disponivel|reserva|aeroporto|bom dia|boa tarde|"
            r"boa noite|olá|oi|gostaria|vocês|voces)\b",
            re.I,
        ),
        2,
    ),
    (
        "fr",
        re.compile(
            r"\b(bonjour|bonsoir|salut|merci|où|comment|chambre|prix|aéroport|"
            r"aeroport|je voudrais|disponible)\b",
            re.I,
        ),
        2,
    ),
    (
        "de",
        re.compile(
            r"\b(hallo|guten tag|guten morgen|guten abend|danke|wo|wie|zimmer|"
            r"preis|flughafen|bitte|verfügbar|verfuegbar)\b",
            re.I,
        ),
        2,
    ),
    (
        "es",
        re.compile(
            r"\b(hola|buenas|buenos|dónde|donde|están|estan|ubicad|cómo|como|llegar|"
            r"aeropuerto|habitaci[oó]n|precio|precios|disponible|reserva|reservar|"
            r"gracias|por favor|quiero|necesito|buenos d[ií]as|buenas tardes|"
            r"buenas noches|ustedes)\b",
            re.I,
        ),
        2,
    ),
]


def _score_language(sample: str) -> dict[str, int]:
    text = (sample or "").strip()
    scores = {"es": 0, "en": 0, "pt": 0, "fr": 0, "de": 0}
    if not text or _SHORT_AFFIRMATION_RE.match(text):
        return scores
    lower = text.lower()
    if re.fullmatch(
        r"(?is)\s*(hello|hi|hey|good morning|good afternoon|good evening)\s*[!?.…]*\s*",
        text,
    ):
        scores["en"] += 3
        return scores
    if (
        re.fullmatch(r"(?is)\s*(oi|olá|ola|bom dia|boa tarde|boa noite)\s*[!?.…]*\s*", text)
        and "hola" not in lower
    ):
        scores["pt"] += 3
        return scores
    if re.fullmatch(r"(?is)\s*(bonjour|bonsoir|salut)\s*[!?.…]*\s*", text):
        scores["fr"] += 3
        return scores
    if re.fullmatch(r"(?is)\s*(hallo|guten tag|guten morgen|guten abend)\s*[!?.…]*\s*", text):
        scores["de"] += 3
        return scores
    if re.fullmatch(
        r"(?is)\s*(hola+|holi+|buenas|buenos d[ií]as|buenas tardes|buenas noches)\s*[!?.…]*\s*",
        text,
    ):
        scores["es"] += 3
        return scores
    for lang, pattern, weight in _LANG_SIGNAL_PATTERNS:
        hits = pattern.findall(lower)
        if hits:
            scores[lang] += weight * len(hits)
    if re.search(r"\b(não|nao|você|voce|obrigad|também|tambem)\b", lower):
        scores["pt"] += 2
    if re.search(r"[áéíóúñ¿¡]", lower):
        scores["es"] += 1
    return scores


def _detect_guest_language(sample: str) -> str | None:
    """Best language for one bubble, or None if too short/ambiguous."""
    scores = _score_language(sample)
    best_lang, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score <= 0:
        return None
    return best_lang


def _conversation_language(guest_turns: list[str], *, default: str = "es") -> str:
    """Detect language from the whole guest chat (greetings + words + thread)."""
    turns = [str(t or "").strip() for t in guest_turns if str(t or "").strip()]
    if not turns:
        return default
    totals = {"es": 0, "en": 0, "pt": 0, "fr": 0, "de": 0}
    last_clear: str | None = None
    for index, turn in enumerate(turns):
        if _SHORT_AFFIRMATION_RE.match(turn):
            continue
        scores = _score_language(turn)
        weight = 1 + index
        for lang, score in scores.items():
            totals[lang] += score * weight
        detected = _detect_guest_language(turn)
        if detected:
            last_clear = detected
    latest = turns[-1]
    latest_lang = _detect_guest_language(latest)
    if latest_lang:
        return latest_lang
    if _SHORT_AFFIRMATION_RE.match(latest) and last_clear:
        return last_clear
    best_lang, best_score = max(totals.items(), key=lambda item: item[1])
    if best_score > 0:
        return best_lang
    return last_clear or default


def _route_language(messages: list[dict[str, Any]]) -> str:
    """Language for map/route copy: follow the whole guest thread."""
    guest_turns: list[str] = []
    for item in messages:
        if item.get("role") != "user":
            continue
        raw = _guest_raw_text(str(item.get("content") or ""))
        if raw:
            guest_turns.append(raw)
    return _conversation_language(guest_turns, default="es")


def _prepared_route_reply(
    route: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    is_follow_up: bool = False,
    model_message: str = "",
) -> str:
    replies = route.get("replies") if isinstance(route.get("replies"), dict) else {}
    language = _route_language(messages)
    # Canned es/en/pt only. Other languages keep DeepSeek's wording.
    if language in {"es", "en", "pt"}:
        text = str(replies.get(language) or "").strip()
    else:
        text = (model_message or "").strip()
    if not text:
        text = str(replies.get("es") or model_message or "").strip()
    if not text:
        return ""
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
    if language not in {"es", "en", "pt"}:
        return text
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
        "Responde corto y ejecutivo (1–2 frases). "
        "Idioma: toda la charla; mensajes cortos heredan el idioma del hilo. No mezcles. "
        "Saludo solo → «Hola, ¿en qué te podemos ayudar?» (o equivalente) y PARA. "
        "Si no sabes → WhatsApp con +56 9… sin link https. "
        "Reserva → WhatsApp corto. No inventes estado. "
        "No te despidas si el huésped no se despidió. "
        "No repitas Barrio Brasil/desayuno si no preguntaron. "
        "Precios solo FAQ: 54990 / 54990 / 69990 / 19990. "
        "Rutas: get_directions_from_hostel. Luego JSON final.",
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
                "en qué te podemos ayudar",
                "en que te podemos ayudar",
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
    skip_duplicate_greeting: bool = True,
) -> dict[str, Any]:
    """Short human hello — never dump a sales brochure on a bare greeting."""
    # People often send "Hola" then "Buenas" as separate bubbles: answer once only.
    # Only skip when the caller says the previous help offer was very recent.
    if skip_duplicate_greeting and is_follow_up and _already_offered_help(prior):
        return {
            "action": "skip",
            "message": "",
            "reason": "duplicate_greeting",
            "model": "rule:greeting",
            "map_image_url": "",
            "maps_url": "",
        }
    prior_texts = [
        str(turn.get("content") or "")
        for turn in (prior or [])
        if turn.get("role") == "user"
    ]
    lang = _conversation_language([*prior_texts, inbound_text], default="es")
    if lang == "en":
        message = "Hi, how can we help you?"
    elif lang == "pt":
        message = "Olá, em que podemos ajudar?"
    elif lang == "fr":
        message = "Bonjour, comment pouvons-nous vous aider ?"
    elif lang == "de":
        message = "Hallo, wie können wir helfen?"
    else:
        message = "Hola, ¿en qué te podemos ayudar?"
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
    prior: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fast path: never hang on DeepSeek when the guest asks to book."""
    info = hostel_info_dict(db)
    phone = whatsapp_phone_display(str(info.get("whatsapp_url") or ""))
    prior_texts = [
        str(turn.get("content") or "")
        for turn in (prior or [])
        if turn.get("role") == "user"
    ]
    lang = _conversation_language([*prior_texts, inbound_text], default="es")
    if lang == "en":
        message = f"Write us on WhatsApp {phone} with dates and guest count."
    elif lang == "pt":
        message = f"Escreva no WhatsApp {phone} com datas e quantas pessoas."
    elif lang == "fr":
        message = f"Écrivez-nous sur WhatsApp {phone} avec dates et nombre de personnes."
    elif lang == "de":
        message = f"Schreib uns auf WhatsApp {phone} mit Daten und Personenanzahl."
    else:
        message = f"Escríbenos por WhatsApp {phone} con fechas y cantidad de personas."
    message = apply_time_greeting(message, is_follow_up=is_follow_up)
    message = sanitize_outbound_message(message)
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
                    "temperature": 0.25,
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
                    model_message=reply_message,
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
            guest_blob = " ".join(
                str(turn.get("content") or "")
                for turn in messages
                if turn.get("role") == "user"
            )
            reply_message = strip_unsolicited_goodbye(reply_message, guest_text=guest_blob)
            reply_message = sanitize_outbound_message(reply_message)
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
    skip_duplicate_greeting: bool = False,
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
            skip_duplicate_greeting=skip_duplicate_greeting,
        )
    if _looks_like_reservation(inbound_text):
        return _reservation_whatsapp_reply(
            db,
            inbound_text=inbound_text,
            is_follow_up=is_follow_up,
            prior=prior,
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
    # Both Instagram DMs and Facebook Messenger are private messages.
    item_type = "message" if channel in {"instagram", "facebook"} else "comment"
    channel_label = "Facebook Messenger" if channel == "facebook" else "Instagram"
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
            prior=prior,
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
