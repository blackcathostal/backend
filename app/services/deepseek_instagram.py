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
from app.services.fns_motor import booking_allowed
from app.services.instagram_knowledge import DEEPSEEK_TOOLS, hostel_info_dict
from app.services.mcp_sources import call_reply_tool
from app.services.reply_context import set_reply_context

SANTIAGO = ZoneInfo("America/Santiago")
# Booking tools hit FNS several times (list/search/cancel/create); 70s left threads stuck.
REPLY_LOOP_TIMEOUT_SECONDS = 140.0
BOOKING_TOOL_TIMEOUT_SECONDS = 95.0
DEFAULT_TOOL_TIMEOUT_SECONDS = 35.0
_BOOKING_TOOL_NAMES = {
    "search_room_availability",
    "check_guest_bookings",
    "create_room_booking",
    "cancel_room_booking",
    "change_room_booking",
}
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
# Chilean guests often write «hola valor» / «valor?» meaning prices — never treat as hello-only.
_PRICE_INTENT_RE = re.compile(
    r"("
    r"\bvalor(?:es)?\b|\bprecios?\b|\btarifas?\b|\brates?\b|\bcostos?\b|\bcostes?\b|"
    r"cu[aá]nto\s+(?:vale|sale|cuesta|cobran|es)|"
    r"how\s+much|price\s*list|nightly\s+rate|per\s+night|"
    r"quanto\s+(?:custa|fica)|pre[cç]o"
    r")",
    re.IGNORECASE,
)
# Ops questions on foreign/collab reels → invite DM. Compliments stay public.
_HOTEL_INQUIRY_RE = re.compile(
    r"("
    r"\bvalor(?:es)?\b|\bprecios?\b|\btarifas?\b|\brates?\b|\bcostos?\b|"
    r"cu[aá]nto\s+(?:vale|sale|cuesta|cobran|es|sale)|"
    r"how\s+much|per\s+night|disponib|availability|"
    r"habitaci[oó]n(?:es)?|dormitor|matrimonial|suite|twin|triple|compartid|"
    r"check[\s-]?in|check[\s-]?out|desayuno|breakfast|wifi|wi[\s-]?fi|"
    r"estacionamiento|parking|aparcamiento|"
    r"ubicaci[oó]n|direcci[oó]n|d[oó]nde\s+(?:quedan|est[aá]n|queda)|"
    r"mensual|por\s+noche|capacidad|cu[aá]ntas?\s+personas|"
    r"reserv(?:a|ar)|booking|book(?:ing)?|voluntari"
    r")",
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
_GOODBYE_ONLY_RE = re.compile(
    r"^\s*(?:[¡!]?\s*)?(?:"
    r"(?:ok|okay|vale|listo|perfecto|gracias|thanks|thank\s+you|merci|obrigad[oa])\s*[,.!]?\s*"
    r")?(?:"
    r"chao+|chau+|adi[oó]s|bye+|goodbye|good\s*bye|hasta\s+luego|hasta\s+pronto|"
    r"nos\s+vemos|cu[ií]date|tchau+|até\s+logo|au\s+revoir"
    r")(?:\s*(?:gracias|thanks|bye|chao|chau))?(?:\s*[,!.…?]*)?(?:\s+(?:👋|🙂|😊|✨))?\s*$",
    re.IGNORECASE,
)
_DISTANCE_QUESTION_RE = re.compile(
    r"("
    r"cu[aá]nto\s+tiempo|"
    r"a\s+cu[aá]nto(?:s|as)?\s+(?:minutos?|km|kil[oó]metros?)|"
    r"qu[eé]\s+tan\s+lejos|"
    r"qu[eé]\s+tan\s+cerca|"
    r"cu[aá]nta\s+distancia|"
    r"distancia\s+(?:a|de|hasta|entre)|"
    r"est[aá]n?\s+(?:muy\s+)?(?:cerca|lejos)|"
    r"queda(?:n)?\s+(?:muy\s+)?(?:cerca|lejos)|"
    r"ubicad[oa]s?\s+(?:de|desde|hasta|a)|"
    r"de\s+aqu[ií]\s+a|"
    r"how\s+far|"
    r"how\s+long\s+(?:to|does|is|from)|"
    r"how\s+many\s+minutes|"
    r"minutes?\s+(?:from|to|away)|"
    r"quanto\s+tempo|"
    r"qu[aã]o\s+longe"
    r")",
    re.IGNORECASE,
)
_VOLUNTEER_RE = re.compile(
    r"("
    r"voluntari[oa]?s?|voluntariado|volunteer(?:ing)?|"
    r"workaway|help[sx]x|woofing|wwoof|"
    r"trabajo\s+gratis|trabajar\s+gratis|alojamiento\s+gratis|"
    r"gratis\s+a\s+cambio|intercambio\s+de\s+trabajo|"
    r"free\s+(?:stay|room|accommodation|volunteer)"
    r")",
    re.IGNORECASE,
)
_SHORT_ACK_RE = re.compile(
    r"^\s*(?:[¡!]?\s*)?(?:"
    r"ok|okay|vale|listo|perfecto|exacto|claro|dale|ya|s[ií]|sip|sep|"
    r"super|súper|genial|buenísimo|buenisimo|entendido|de\s+acuerdo|"
    r"ah\s+ok|ah\s+ya|mm+|aha+|aja+"
    r")(?:\s*[,!.…?]*)?(?:\s+(?:👋|🙂|😊|✨|👍|🙏))?\s*$",
    re.IGNORECASE,
)
# "Gracias!" alone is a soft closing — answer with a short goodbye, don't skip.
_THANKS_ONLY_RE = re.compile(
    r"^\s*(?:[¡!]?\s*)?(?:"
    r"gracias(?:\s+mil|\s+totales?)?|thanks(?:\s+a\s+lot|\s+so\s+much)?|"
    r"thank\s+you(?:\s+so\s+much)?|merci(?:\s+beaucoup)?|"
    r"obrigad[oa](?:\s+mesmo)?"
    r")(?:\s*[,!.…?]*)?(?:\s+(?:👋|🙂|😊|✨|👍|🙏))?\s*$",
    re.IGNORECASE,
)
_DOC_ID_RE = re.compile(
    r"("
    r"c[eé]dula|pasaporte|documento\s+de\s+identidad|carnet|"
    r"rut\b|dni\b|id\s+card|identity\s+card"
    r")",
    re.IGNORECASE,
)
MAX_REPLY_CHARS = 280
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
- UN solo mensaje por turno. Nunca mandes dos burbujas seguidas ni un “Exacto/Claro…” aparte.
- Respuestas EJECUTIVAS: 1 frase (máx. 2). Máx. ~220 caracteres. Saludos ~60.
- Habla natural. Máximo 1 emoji y solo si encaja.
- NO listes todas las habitaciones ni todas las tarifas salvo que pidan el listado completo.
  Si preguntan por una (ej. matrimonial), responde SOLO esa.
- NO confirmes de más (“Exacto, ese es el valor”). Si ya diste el dato, no reenvíes otro mensaje.
- Si el huésped solo dice ok/vale/exacto/gracias/sí → no hace falta responder (o una palabra).
- NO repitas Barrio Brasil/desayuno/boutique salvo que pregunten eso.
- NUNCA te despidas salvo que el huésped se despida primero.
- Saludo solo → «Hola, ¿en qué te podemos ayudar?» y PARA.
- Voluntariado / trabajo gratis / stay gratis → amable y corto: no se está aceptando.
- Cédula/documentos/beneficios dudosos → deriva a WhatsApp (no inventes).
- No asumas aeropuerto/transporte si no lo dijo.

Idioma (OBLIGATORIO):
- Detecta el idioma por toda la charla y responde SOLO en ese idioma.
- Mensajes cortos (“sí”, “ok”) heredan el idioma del hilo. No mezcles idiomas.

Datos (OBLIGATORIO):
- Precios / tipos / descripción / servicios de habitación → SOLO search_rooms (CRUD).
  NUNCA inventes habitaciones ni precios. Si search_rooms no trae dato, dilo.
- Fotos de habitaciones → get_room_photos (o photo_urls de search_rooms). El sistema
  manda las imágenes en el DM. Mensaje corto (“Te mando las fotos…”). NO mandes a WhatsApp
  solo por fotos. Si no hay fotos cargadas, di blackcathostal.com (Habitaciones).
- NO digas precios ni listes habitaciones a menos que el huésped lo pida explícitamente.
  Si pide “precios”, llama search_rooms y resume SOLO lo del CRUD; detalle por tipo SOLO si pregunta por ese tipo.
- Mensual → search_common_answers topic mensual (no inventes).
- Check-in, check-out, desayuno, estacionamiento, WhatsApp, dirección → get_hostel_info.
- Otras dudas del hostal → search_common_answers (no habitaciones).
- WhatsApp: dalo MÁXIMO UNA vez por conversación (enlace; se envía sin imagen).
  Si ya lo diste en el hilo, NO lo vuelvas a mencionar ni cierres con “por WhatsApp”.
- NUNCA digas que vas a confirmar con recepción, ni que hablas con recepción, ni que “recibimos
  tu mensaje en recepción”. Este canal NO habla con recepción.
- Si NO sabes: deriva a WhatsApp (si aún no lo diste) o pide el dato faltante. Breve.
- Tiempo/distancia → get_directions_from_hostel eta_only=true: solo minutos en auto, sin mapa.
- Cómo llegar → eta_only=false. “Te lo indico en el siguiente mapa.” Sin URLs.
- Si el huésped se despide, despídete corto. Si no, no te despidas.
- Reserva → invita a WhatsApp con fechas y personas (número solo si aún no salió en el hilo).
  Nunca inventes estado de reserva.
- Disponibilidad/cupo → SOLO tras search_room_availability (FNS). NUNCA inventes cupo,
  “sí hay después”, ni fechas alternativas sin volver a consultar FNS. Si FNS dice no hay
  cupo, dilo; no contradigas ni inventes disponibilidad.
- Compartida: di «Tenemos compartidas solo para hombres y solo para mujeres; ¿cuál
  requieres?». No preguntes orientación sexual ni fuerces «¿eres hombre o mujer?».
- Fechas: usa SIEMPRE año actual o futuro (zona America/Santiago). Si el huésped dice
  «diciembre» / «1 al 3» sin año, o «este año», NO uses un año pasado. Hoy manda.
- Tarifas noche (solo si preguntan): Matrimonial/Suite $54.990, Twin $54.990, Triple $69.990, cama $19.990.
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
            if (
                text
                and "EJECUTIVAS" in text
                and "NUNCA te despidas" in text
                and "máximo UNA vez" in text.lower()
                and "1.500.000" in text
            ):
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


def whatsapp_digits(whatsapp_url: str = "") -> str:
    raw = (whatsapp_url or "").strip()
    for pattern in (_WA_ME_RE, _WA_ME_PLAIN_RE, _SHARE_WA_RE):
        match = pattern.search(raw)
        if match:
            return match.group(1)
    digits = re.sub(r"\D", "", raw)
    # Prefer a Chile mobile block if the string had a long digit soup.
    chile = re.search(r"56\d{9,11}", digits)
    if chile:
        return chile.group(0)
    if digits.startswith("56") and len(digits) >= 11:
        return digits[:11] if len(digits) > 13 else digits
    if digits.startswith("9") and len(digits) == 9:
        return f"56{digits}"
    return digits or "56949105984"


def whatsapp_direct_url(whatsapp_url: str = "") -> str:
    """Real WhatsApp deep link (only for buttons / landing redirect, never IG text)."""
    return f"https://wa.me/{whatsapp_digits(whatsapp_url)}"


def whatsapp_click_url(whatsapp_url: str = "") -> str:
    """Public share link without WhatsApp OG image (safe to put in IG text)."""
    from app.services.short_links import whatsapp_share_url

    return whatsapp_share_url(whatsapp_digits(whatsapp_url))


def whatsapp_phone_display(whatsapp_url: str = "") -> str:
    """Human phone fallback when a plain number is needed."""
    digits = whatsapp_digits(whatsapp_url)
    if digits.startswith("56") and len(digits) >= 11:
        rest = digits[2:]
        if rest.startswith("9") and len(rest) == 9:
            return f"+56 9 {rest[1:5]} {rest[5:]}"
        return f"+56 {rest}"
    if digits:
        return f"+{digits}"
    return "+56 9 4910 5984"


_RECEPTION_CLAIM_RE = re.compile(
    r"(?is)(?:^|[.!?…]\s*)("
    r"(?:lo\s+)?(?:voy\s+a\s+|vamos\s+a\s+|te\s+)?"
    r"confirm(?:o|amos|aré|are|ando)?\s+(?:eso\s+)?(?:con|en)\s+recepci[oó]n|"
    r"(?:hablo|hablamos|consulto|consultamos|lo\s+paso|se\s+lo\s+paso)\s+"
    r"(?:con|a)\s+recepci[oó]n|"
    r"recepci[oó]n\s+(?:te\s+)?(?:confirma|confirma\s+all[ií]|te\s+responde)|"
    r"(?:ya\s+)?(?:lo\s+)?(?:vi|vimos|recibimos)\s+(?:en|por)\s+recepci[oó]n"
    r")[^.!?…]*[.!?…]?"
)
_PHONE_AS_WA_RE = re.compile(
    r"(?<![/\d\w])(?:\+?56\s*9\s*\d{4}\s*\d{4}|\+?569\d{8})(?!\w)"
)
_SHARE_WA_RE = re.compile(
    r"https?://(?:api\.)?blackcathostal\.com/api/w/(56\d{8,12})\S*",
    re.IGNORECASE,
)
_ANY_WA_TOKEN_RE = re.compile(
    r"(?:"
    r"https?://(?:api\.)?blackcathostal\.com/api/w/56\d{8,12}\S*"
    r"|https?://(?:wa\.me/|api\.whatsapp\.com/send\?phone=)\d{8,15}\S*"
    r"|wa\.me/\d{8,15}"
    r"|(?<![/\d\w])(?:\+?56\s*9\s*\d{4}\s*\d{4}|\+?569\d{8})(?!\w)"
    r")",
    re.IGNORECASE,
)


def sanitize_outbound_message(message: str) -> str:
    """Normalize WhatsApp mentions to ONE /api/w/… link (no WhatsApp OG image)."""
    text = message or ""
    found = _ANY_WA_TOKEN_RE.search(text)
    if found:
        share = whatsapp_click_url(found.group(0))
        text = _ANY_WA_TOKEN_RE.sub(" ", text)
        text = f"{text.strip()} {share}".strip()
    text = _RECEPTION_CLAIM_RE.sub(" ", text)
    return re.sub(r"\s{2,}", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()


def extract_whatsapp_link(message: str) -> tuple[str, str]:
    """Split into (text_without_url, direct https://wa.me/... for button, or '')."""
    text = message or ""
    match = _ANY_WA_TOKEN_RE.search(text)
    if not match:
        return text.strip(), ""
    direct = whatsapp_direct_url(match.group(0))
    cleaned = _ANY_WA_TOKEN_RE.sub(" ", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,;-")
    return cleaned, direct


def thread_already_shared_whatsapp(prior: list[dict[str, Any]] | None) -> bool:
    for turn in prior or []:
        if turn.get("role") != "assistant":
            continue
        text = str(turn.get("content") or "").lower()
        if (
            "whatsapp" in text
            or "+56" in text
            or "wa.me" in text
            or "/api/w/" in text
        ):
            return True
    return False


_WHATSAPP_PHRASE_RE = re.compile(
    r"(?is)"
    r"(?:^|[.!?…,;:\s]+)"
    r"(?:(?:escr[ií]benos|escribe|cont[aá]ctanos|contin[uú]a|sigue|confirma(?:lo)?|"
    r"resuelve|cotiza|reserva)\s+)?"
    r"(?:por|en|via|vía|on|pelo)\s+whatsapp"
    r"|(?:^|[.!?…,;:\s]+)whatsapp\.?"
)


def strip_repeated_whatsapp_number(message: str, *, already_shared: bool) -> str:
    """If WhatsApp was already shared, strip links/mentions — never append «por WhatsApp»."""
    text = message or ""
    if not already_shared:
        # Drop empty trailing «por WhatsApp» tacked on by the model for no reason.
        text = re.sub(
            r"(?is)(?:[.!?…,;:\s]+)?(?:por|en|via|vía|on|pelo)\s+whatsapp\.?\s*$",
            "",
            text,
        )
        return re.sub(r"\s{2,}", " ", text).strip(" ,;-")
    text = _ANY_WA_TOKEN_RE.sub("", text)
    text = _WA_ME_RE.sub("", text)
    text = _WA_ME_PLAIN_RE.sub("", text)
    text = _SHARE_WA_RE.sub("", text)
    text = re.sub(r"\+56\s*9\s*\d{4}\s*\d{4}", "", text)
    text = re.sub(r"\+56\s*\d[\d\s-]{6,}\d", "", text)
    text = _WHATSAPP_PHRASE_RE.sub("", text)
    text = re.sub(r"(?i)\bwhatsapp\b", "", text)
    text = re.sub(r"(?is)(?:^|[.!?…])\s*(?:por|en|via|vía)\s*$", "", text)
    return re.sub(r"\s{2,}", " ", text).strip(" ,;-.")


def enforce_single_short_reply(message: str, *, max_chars: int = MAX_REPLY_CHARS) -> str:
    """One short bubble: collapse paragraphs and hard-cap length."""
    text = (message or "").strip()
    if not text:
        return ""
    # Model sometimes fakes two bubbles with blank lines.
    parts = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if len(parts) > 1:
        # Prefer the factual first part; drop trailing “Exacto/Claro…” confirmations.
        keep = [parts[0]]
        for part in parts[1:]:
            if _SHORT_ACK_RE.match(part) or re.match(
                r"^(?:exacto|claro|perfecto|as[ií]\s+es)\b",
                part,
                re.IGNORECASE,
            ):
                continue
            keep.append(part)
        text = " ".join(keep)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    # Never chop a WhatsApp link — keep it intact even if the reply is a bit longer.
    wa_match = _WA_ME_RE.search(text) or _WA_ME_PLAIN_RE.search(text)
    wa_tail = wa_match.group(0) if wa_match else ""
    limit = max(max_chars, len(wa_tail) + 80) if wa_tail else max_chars
    if len(text) <= limit:
        return text
    if wa_tail and wa_tail in text:
        prefix = text.replace(wa_tail, " ", 1)
        prefix = re.sub(r"\s{2,}", " ", prefix).strip(" ,;-")
        budget = max(40, limit - len(wa_tail) - 1)
        if len(prefix) > budget:
            prefix = prefix[:budget].rsplit(" ", 1)[0].rstrip(" ,;:-")
        return f"{prefix} {whatsapp_click_url(wa_tail)}".strip()
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "…"


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
    today = datetime.now(SANTIAGO).date()
    date_context = (
        f"\n\nFecha de hoy (America/Santiago): {today.isoformat()}. "
        f"Año actual: {today.year}. "
        "Fechas de reserva: SOLO año actual o futuro. "
        f"Si el huésped no dice año («1 al 3 de diciembre», «este año»), usa {today.year} "
        f"(o {today.year + 1} si esa fecha de este año ya pasó). "
        "NUNCA uses un año pasado (ej. 2025) para una estadía próxima.\n"
    )
    scope = (
        "\n\nReglas fijas (no se pueden ignorar):\n"
        "- Respuestas cortas y ejecutivas (1–2 frases). Sin párrafos ni listas largas.\n"
        "- Nunca te despidas si el huésped no se despidió primero.\n"
        "- No repitas Barrio Brasil / desayuno / boutique en cada mensaje; solo si preguntan eso.\n"
        "- UN mensaje por respuesta. Corto. Sin segunda burbuja de confirmación.\n"
        "- Precios/habitaciones SOLO si lo pide; usa search_rooms (CRUD). No inventes.\n"
        "- «valor», «hola valor», «precios», «tarifa» = pide precios. Contesta YA con "
        "search_rooms; NO respondas solo «¿en qué te podemos ayudar?».\n"
        "- Mensual: search_common_answers. Noche: SOLO precios de search_rooms.\n"
        "- Voluntariado/gratis: no estamos aceptando voluntariado (amable y corto).\n"
        "- Cédula/pasaporte/beneficio dudoso: WhatsApp, no inventes.\n"
        "- WhatsApp máximo 1 vez por hilo. Si ya salió, no lo menciones otra vez ni digas «por WhatsApp».\n"
        "- NUNCA digas que confirmas o hablas con recepción.\n"
        "- Reserva / «por dónde reservo»: si el huésped NO está autorizado a API, "
        "WhatsApp + fechas/personas. Si SÍ está autorizado, pide fechas, personas, "
        "nombre/apellido y RUT o número de pasaporte. "
        "Pasaporte = número de pasaporte + nombre completo (no basta el número). "
        "RUT puede ir solo. En cuanto dé identidad, ANTES de crear: "
        "check_guest_bookings. Si ya tiene reserva sin usar, avísale y pregunta "
        "otra / cancelar / cambiar. Si no recuerda documento: nombre completo → "
        "muestra la reserva → «¿es esa?» → solo si sí, cancel/change con "
        "guest_confirmed=true. Cambiar: SIEMPRE change_room_booking "
        "(nunca create+confirm_extra; eso no borra la vieja). "
        "En IG/FB cotiza SOLO CLP de search_rooms (CRUD). No inventes tarifas. "
        "No digas USD. Nunca digas «por ahí». "
        "Compartida: di «Tenemos compartidas solo para hombres y solo para mujeres; "
        "¿cuál requieres?» (no preguntes orientación ni fuerces «¿eres hombre o mujer?»).\n"
        "- Disponibilidad/cupo: OBLIGATORIO search_room_availability (FNS) antes de "
        "afirmar. NUNCA inventes cupo, «sí hay después» ni otras fechas sin consultar "
        "FNS de nuevo. Si FNS dice NO HAY CUPO, dilo y no lo contradigas.\n"
        "- Fechas YYYY-MM-DD en search_room_availability/create/change: año actual o "
        "futuro según la fecha de hoy de arriba. Nunca un año pasado.\n"
        "- Tiempo/distancia: eta_only=true → solo min en auto, SIN mapa.\n"
        "- Si se despide, despídete corto. Si no, no.\n"
        "- Instagram y Facebook: mismas reglas.\n"
    )
    if is_follow_up:
        greeting = (
            "\nEsta NO es la primera respuesta. NO saludes de nuevo. "
            "Responde solo a lo nuevo. NO repitas datos ni frases ya dichas."
        )
    else:
        greeting = (
            "\nPrimer mensaje. Si SOLO dijo hola/hi/buenas (nada más): "
            "«Hola, ¿en qué te podemos ayudar?» y PARA. "
            "Si dice hola + valor/precio/tarifa/consulta: saluda en la misma frase "
            "y contesta el dato (precios si pidió valor). Nunca ignores la consulta."
        )
    return get_system_prompt().rstrip() + date_context + scope + greeting + (extra or "")


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
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    # Model sometimes replies in plain text after tools — don't leave the DM stuck.
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE).strip()
    if cleaned and not cleaned.startswith("{"):
        return {
            "action": "reply",
            "message": cleaned,
            "reason": "plain_text_fallback",
        }
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
    if item_type == "mention":
        return "mención (@) en reel/post ajeno"
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
    wrote_comment = "sí" if comments or item_type in {"comment", "mention"} else "no"
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
        "UN solo mensaje corto. Sin segunda burbuja. "
        "Saludo solo (sin valor/precio) → «Hola, ¿en qué te podemos ayudar?» y PARA. "
        "«hola valor» / valor / precios → search_rooms y da tarifas del CRUD YA. "
        "Una tarifa si preguntan un tipo; no inventes. Mensual: FAQ. "
        "Voluntariado → no aceptamos (corto). Cédula dudosa → WhatsApp una vez. "
        "Si ya diste WhatsApp, no lo repitas ni cierres con «por WhatsApp». Sin recepción. "
        "Tiempo/distancia → eta_only=true. Si se despide → despídete. JSON final.",
    ]
    return "\n".join(lines)


def _looks_like_reservation(text: str) -> bool:
    sample = (text or "").strip()
    if not sample:
        return False
    return bool(_RESERVATION_RE.search(sample))


def _strip_ig_mentions(text: str) -> str:
    """Remove @handles so '@hotel hola valor' is classified by the real words."""
    cleaned = re.sub(r"@[\w.]+", " ", text or "", flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


def _looks_like_price_question(text: str) -> bool:
    """True when the guest asks for rates («valor», precios, how much…)."""
    sample = _strip_ig_mentions(text or "")
    if not sample:
        return False
    return bool(_PRICE_INTENT_RE.search(sample))


def looks_like_hotel_inquiry(text: str) -> bool:
    """Prices, rooms, booking, location, amenities — not social compliments."""
    sample = _strip_ig_mentions(text or "")
    if not sample:
        return False
    if _looks_like_price_question(sample) or _looks_like_reservation(sample):
        return True
    return bool(_HOTEL_INQUIRY_RE.search(sample))


def _looks_like_greeting_only(text: str) -> bool:
    """True for one or several greeting bubbles sent in a burst (hola + buenas)."""
    sample = _strip_ig_mentions(text or "")
    if not sample or len(sample) > 120:
        return False
    # «Hola valor» is a price ask, not a bare hello.
    if _looks_like_price_question(sample):
        return False
    lines = [line.strip() for line in sample.splitlines() if line.strip()]
    if not lines:
        return False
    return all(bool(_GREETING_ONLY_RE.match(line)) for line in lines)


def _format_clp(amount: int) -> str:
    return f"${amount:,}".replace(",", ".")


def _price_summary_reply(
    *,
    inbound_text: str,
    is_follow_up: bool,
    prior: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Deterministic rates from Rooms CRUD — never invent prices."""
    prior_texts = [
        str(turn.get("content") or "")
        for turn in (prior or [])
        if turn.get("role") == "user"
    ]
    lang = _conversation_language([*prior_texts, inbound_text], default="es")
    from app.core.database import SessionLocal
    from app.services.instagram_knowledge import search_rooms

    with SessionLocal() as db:
        rooms = search_rooms(db)
    bits: list[str] = []
    for room in rooms:
        name = str(room.get("name") or room.get("type") or "").strip()
        price = int(room.get("price_clp") or 0)
        if not name or price <= 0:
            continue
        bits.append(f"{name} {_format_clp(price)}")
    if not bits:
        body = (
            "We don't have room rates loaded yet."
            if lang == "en"
            else "Aún no hay tarifas de habitaciones cargadas en el panel."
        )
    elif lang == "en":
        body = "From " + ", ".join(bits) + " (per night)."
    elif lang == "pt":
        body = "A partir de " + ", ".join(bits) + " (por noite)."
    else:
        body = "Desde " + ", ".join(bits) + " (noche)."
    if bits and not is_follow_up:
        if lang == "en":
            message = f"Hi! {body}"
        elif lang == "pt":
            message = f"Olá! {body}"
        else:
            message = f"Hola! {body}"
    else:
        message = body
    return {
        "action": "reply",
        "message": message,
        "reason": "price_question",
        "model": "rule:prices_rooms_crud",
        "map_image_url": "",
        "maps_url": "",
    }


def _looks_like_thanks_only(text: str) -> bool:
    sample = (text or "").strip()
    if not sample or len(sample) > 80:
        return False
    lines = [line.strip() for line in sample.splitlines() if line.strip()]
    if not lines:
        return False
    return all(bool(_THANKS_ONLY_RE.match(line)) for line in lines)


def _looks_like_goodbye_only(text: str) -> bool:
    sample = (text or "").strip()
    if not sample or len(sample) > 120:
        return False
    lines = [line.strip() for line in sample.splitlines() if line.strip()]
    if not lines:
        return False
    if all(bool(_GOODBYE_ONLY_RE.match(line)) for line in lines):
        return True
    # Closing thanks after a resolved booking/help should get a short farewell.
    return _looks_like_thanks_only(sample)


def _looks_like_distance_question(text: str) -> bool:
    sample = (text or "").strip()
    if not sample:
        return False
    return bool(_DISTANCE_QUESTION_RE.search(sample))


def _looks_like_short_ack(text: str) -> bool:
    sample = (text or "").strip()
    if not sample or len(sample) > 80:
        return False
    lines = [line.strip() for line in sample.splitlines() if line.strip()]
    if not lines:
        return False
    return all(bool(_SHORT_ACK_RE.match(line)) for line in lines)


def _looks_like_volunteer(text: str) -> bool:
    return bool(_VOLUNTEER_RE.search(text or ""))


def _looks_like_doc_id_question(text: str) -> bool:
    sample = (text or "").strip()
    if not sample:
        return False
    if not _DOC_ID_RE.search(sample):
        return False
    # Birthday FAQ already covers cédula for that benefit; other ID asks → WhatsApp.
    if re.search(r"cumplea[nñ]os|birthday|fans?", sample, re.IGNORECASE):
        return False
    return True


def _skip_reply(reason: str) -> dict[str, Any]:
    return {
        "action": "skip",
        "message": "",
        "reason": reason,
        "model": f"rule:{reason}",
        "map_image_url": "",
        "maps_url": "",
    }


def _volunteer_reply(
    *,
    inbound_text: str,
    prior: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prior_texts = [
        str(turn.get("content") or "")
        for turn in (prior or [])
        if turn.get("role") == "user"
    ]
    lang = _conversation_language([*prior_texts, inbound_text], default="es")
    if lang == "en":
        message = "We're not accepting volunteers right now."
    elif lang == "pt":
        message = "No momento não estamos aceitando voluntariado."
    else:
        message = "Por ahora no estamos aceptando voluntariado."
    return {
        "action": "reply",
        "message": message,
        "reason": "volunteer_closed",
        "model": "rule:volunteer",
        "map_image_url": "",
        "maps_url": "",
    }


def _unknown_whatsapp_reply(
    db: Session,
    *,
    inbound_text: str,
    is_follow_up: bool,
    prior: list[dict[str, Any]] | None = None,
    reason: str = "unknown_whatsapp",
) -> dict[str, Any]:
    info = hostel_info_dict(db)
    link = whatsapp_click_url(str(info.get("whatsapp_url") or ""))
    already = thread_already_shared_whatsapp(prior)
    prior_texts = [
        str(turn.get("content") or "")
        for turn in (prior or [])
        if turn.get("role") == "user"
    ]
    lang = _conversation_language([*prior_texts, inbound_text], default="es")
    if lang == "en":
        message = (
            "Please confirm that there."
            if already
            else f"Please confirm that on WhatsApp {link}"
        )
    elif lang == "pt":
        message = (
            "Confirma isso por lá."
            if already
            else f"Confirma isso pelo WhatsApp {link}"
        )
    else:
        message = (
            "Eso debes confirmarlo por ahí."
            if already
            else f"Eso debes confirmarlo por WhatsApp {link}"
        )
    message = apply_time_greeting(message, is_follow_up=is_follow_up)
    message = sanitize_outbound_message(message)
    return {
        "action": "reply",
        "message": message,
        "reason": reason,
        "model": f"rule:{reason}",
        "map_image_url": "",
        "maps_url": "",
    }


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


def _goodbye_reply(
    *,
    inbound_text: str,
    prior: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prior_texts = [
        str(turn.get("content") or "")
        for turn in (prior or [])
        if turn.get("role") == "user"
    ]
    lang = _conversation_language([*prior_texts, inbound_text], default="es")
    if lang == "en":
        message = "Bye! Take care."
    elif lang == "pt":
        message = "Tchau! Cuide-se."
    elif lang == "fr":
        message = "Au revoir !"
    elif lang == "de":
        message = "Tschüss! Bis bald."
    else:
        message = "¡Chao! Que estés bien."
    return {
        "action": "reply",
        "message": message,
        "reason": "goodbye",
        "model": "rule:goodbye",
        "map_image_url": "",
        "maps_url": "",
    }


def _short_driving_eta_message(route: dict[str, Any], language: str) -> str:
    replies = route.get("replies") if isinstance(route.get("replies"), dict) else {}
    if language in replies and str(replies.get(language) or "").strip():
        return str(replies[language]).strip()
    minutes = route.get("duration_minutes")
    if minutes is None:
        minutes = _spoken_minutes_from_duration(str(route.get("duration") or ""))
    if minutes is None:
        return str(replies.get("es") or route.get("summary") or "").strip()
    if language == "en":
        return f"About {minutes} min by car."
    if language == "pt":
        return f"Fica a uns {minutes} min de carro."
    if language == "fr":
        return f"Environ {minutes} min en voiture."
    if language == "de":
        return f"Etwa {minutes} Min. mit dem Auto."
    return f"Estás a unos {minutes} min en auto."


def _spoken_minutes_from_duration(duration: str) -> int | None:
    text = str(duration or "").strip().lower()
    if not text:
        return None
    total = 0
    found = False
    for match in re.finditer(r"(\d+)\s*(h|hr|hora|horas|min|mins|minute|minutes)?", text):
        value = int(match.group(1))
        unit = (match.group(2) or "").lower()
        if unit.startswith("h"):
            total += value * 60
            found = True
        elif unit.startswith("min") or not unit:
            total += value
            found = True
    return total if found else None


_WHERE_TO_BOOK_RE = re.compile(
    r"("
    r"por\s+d[oó]nde\s+reserv|"
    r"d[oó]nde\s+reserv|"
    r"c[oó]mo\s+reserv|"
    r"where\s+(?:do\s+i\s+)?(?:book|reserve)|"
    r"how\s+(?:do\s+i\s+)?(?:book|reserve)|"
    r"link\s+(?:de\s+)?(?:reserva|whatsapp)|"
    r"cual\s+es\s+el\s+whatsapp|"
    r"cu[aá]l\s+es\s+el\s+whatsapp|"
    r"pasame\s+el\s+whatsapp|"
    r"n[uú]mero\s+de\s+whatsapp"
    r")",
    re.IGNORECASE,
)


def _looks_like_where_to_book(text: str) -> bool:
    return bool(_WHERE_TO_BOOK_RE.search(text or ""))


def _reservation_whatsapp_reply(
    db: Session,
    *,
    inbound_text: str,
    is_follow_up: bool,
    prior: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fast path: booking / where-to-reserve always includes the WhatsApp link."""
    info = hostel_info_dict(db)
    link = whatsapp_click_url(str(info.get("whatsapp_url") or ""))
    prior_texts = [
        str(turn.get("content") or "")
        for turn in (prior or [])
        if turn.get("role") == "user"
    ]
    lang = _conversation_language([*prior_texts, inbound_text], default="es")
    # Never answer “por ahí” without the link — guests ask again (see @emkrypted).
    if lang == "en":
        message = f"Reserve on WhatsApp with your dates and guest count: {link}"
    elif lang == "pt":
        message = f"Reserve no WhatsApp com datas e quantas pessoas: {link}"
    elif lang == "fr":
        message = f"Réservez sur WhatsApp avec dates et nombre de personnes: {link}"
    elif lang == "de":
        message = f"Reserviere per WhatsApp mit Daten und Personenanzahl: {link}"
    else:
        message = f"Reserva por WhatsApp con fechas y cantidad de personas: {link}"
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


_AVAILABILITY_CLAIM_RE = re.compile(
    r"(?:"
    r"hay\s+(?:cupo|disponib)|"
    r"tenemos\s+(?:cupo|disponib)|"
    r"s[ií]\s+hay|"
    r"available|"
    r"we\s+have\s+(?:availability|rooms?|beds?)|"
    r"no\s+hay\s+(?:cupo|disponib)|"
    r"sin\s+cupo|"
    r"sold\s+out|"
    r"para\s+(?:el|la|los|las)\s+\d|"
    r"otra(?:s)?\s+fecha|"
    r"otros?\s+d[ií]as|"
    r"la\s+semana\s+siguiente|"
    r"m[aá]s\s+adelante\s+hay|"
    r"despu[eé]s\s+hay|"
    r"hay\s+despu[eé]s"
    r")",
    re.IGNORECASE,
)
_POSITIVE_AVAIL_RE = re.compile(
    r"(?:"
    r"hay\s+(?:cupo|disponib)|"
    r"tenemos\s+(?:cupo|disponib)|"
    r"s[ií]\s+hay|"
    r"available|"
    r"we\s+have\s+(?:availability|rooms?|beds?)|"
    r"m[aá]s\s+adelante\s+hay|"
    r"despu[eé]s\s+hay|"
    r"hay\s+despu[eé]s"
    r")",
    re.IGNORECASE,
)


_GUEST_DATE_INTENT_RE = re.compile(
    r"(?:"
    r"\b\d{1,2}(?:\s*(?:al|-|/)\s*\d{1,2})?\b|"
    r"\b(?:ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic|"
    r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"septiembre|octubre|noviembre|diciembre|"
    r"january|february|march|april|june|july|august|"
    r"september|october|november|december)\b|"
    r"disponib|cupo|compartida|reserv"
    r")",
    re.IGNORECASE,
)


def _fns_availability_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    rooms = [row for row in (result.get("rooms") or []) if isinstance(row, dict)]
    available = bool(result.get("available")) and bool(rooms)
    summary = str(result.get("availability_summary") or result.get("message") or "")
    if "NO HAY CUPO" in summary.upper():
        available = False
    shared_available = bool(result.get("shared_available")) and available
    return {
        "checked": True,
        "ok": result.get("ok", True) is not False,
        "available": available,
        "shared_available": shared_available,
        "summary": summary,
        "start_date": str(result.get("start_date") or ""),
        "end_date": str(result.get("end_date") or ""),
        "date_normalized": bool(result.get("date_normalized")),
        "original_start_date": str(result.get("original_start_date") or ""),
        "original_end_date": str(result.get("original_end_date") or ""),
        "source": str(result.get("source") or "fns_motor_search_availability"),
    }


def _guest_blob_from_messages(messages: list[dict[str, Any]]) -> str:
    return " ".join(
        _guest_raw_text(str(turn.get("content") or ""))
        for turn in messages
        if turn.get("role") == "user"
    )


async def _run_reply_loop(
    *,
    db: Session,
    messages: list[dict[str, Any]],
    base: str,
    model: str,
    headers: dict[str, str],
    is_follow_up: bool = False,
    enforce_fns_availability: bool = False,
) -> dict[str, Any]:
    route_reply: dict[str, Any] | None = None
    room_photo_urls: list[str] = []
    last_fns_availability: dict[str, Any] | None = None
    availability_nudge_sent = False

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
                    tool_timeout = (
                        BOOKING_TOOL_TIMEOUT_SECONDS
                        if name in _BOOKING_TOOL_NAMES
                        else DEFAULT_TOOL_TIMEOUT_SECONDS
                    )
                    try:
                        result = await asyncio.wait_for(
                            call_reply_tool(name, args),
                            timeout=tool_timeout,
                        )
                    except asyncio.TimeoutError:
                        result = {
                            "ok": False,
                            "error": (
                                f"La tool {name} tardó demasiado "
                                f"(>{int(tool_timeout)}s). "
                                "Di al huésped que reintente en un momento; "
                                "NO inventes cupo ni digas que no hay cupo."
                            ),
                            "timeout": True,
                        }
                    if (
                        name == "search_room_availability"
                        and isinstance(result, dict)
                        and result.get("ok") is not False
                        and not result.get("timeout")
                    ):
                        last_fns_availability = _fns_availability_snapshot(result)
                    if (
                        name == "get_directions_from_hostel"
                        and isinstance(result, dict)
                        and result.get("ok")
                    ):
                        route_reply = result
                    if (
                        name == "get_room_photos"
                        and isinstance(result, dict)
                        and result.get("ok")
                    ):
                        urls = [
                            str(u).strip()
                            for u in (result.get("image_urls") or [])
                            if str(u).strip().startswith("https://")
                        ]
                        if not urls:
                            for row in result.get("photos") or []:
                                if isinstance(row, dict):
                                    u = str(row.get("image_url") or "").strip()
                                    if u.startswith("https://"):
                                        urls.append(u)
                        if urls:
                            room_photo_urls = urls[:6]
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

            # Hard stop: never invent FNS availability. Must call MCP
            # search_room_availability (FNS Motor) before yes/no cupo.
            guest_blob = _guest_blob_from_messages(messages)
            guest_wants_dates = bool(_GUEST_DATE_INTENT_RE.search(guest_blob))
            claims_availability = bool(
                _AVAILABILITY_CLAIM_RE.search(reply_message or "")
            )
            if (
                enforce_fns_availability
                and action == "reply"
                and (claims_availability or guest_wants_dates)
            ):
                today = datetime.now(SANTIAGO).date().isoformat()
                if last_fns_availability is None and not availability_nudge_sent:
                    availability_nudge_sent = True
                    messages.append(
                        {
                            "role": "assistant",
                            "content": content,
                        }
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "STOP: disponibilidad SOLO con la tool MCP "
                                "search_room_availability (endpoint FNS Motor). "
                                f"Hoy en Santiago: {today}. "
                                "Fechas YYYY-MM-DD en año actual o futuro; "
                                "NUNCA un año pasado. "
                                "Llama search_room_availability ahora con las fechas "
                                "del huésped y responde SOLO con availability_summary. "
                                "Prohibido inventar cupo o decir no hay sin ese resultado."
                            ),
                        }
                    )
                    continue
                if last_fns_availability is not None:
                    start = last_fns_availability.get("start_date") or ""
                    end = last_fns_availability.get("end_date") or ""
                    if last_fns_availability.get("available"):
                        if (
                            re.search(
                                r"no\s+hay\s+(?:cupo|disponib)|sin\s+cupo|sold\s+out",
                                reply_message or "",
                                re.IGNORECASE,
                            )
                            and last_fns_availability.get("shared_available")
                        ):
                            reply_message = (
                                f"Sí hay cupo en compartida {start} al {end}. "
                                "Tenemos compartidas solo para hombres y solo para "
                                "mujeres; ¿cuál requieres?"
                            )
                            reason = reason or "fns_shared_available_enforced"
                        elif (
                            re.search(
                                r"no\s+hay\s+(?:cupo|disponib)|sin\s+cupo|sold\s+out",
                                reply_message or "",
                                re.IGNORECASE,
                            )
                            and last_fns_availability.get("available")
                        ):
                            summary = str(
                                last_fns_availability.get("summary") or ""
                            ).strip()
                            reply_message = (
                                summary[:220]
                                if summary
                                else f"Sí hay cupo para {start} al {end}."
                            )
                            reason = reason or "fns_available_enforced"
                    elif _POSITIVE_AVAIL_RE.search(reply_message or ""):
                        if start and end:
                            reply_message = f"No hay cupo para {start} → {end}."
                        else:
                            reply_message = "No hay cupo para esas fechas."
                        reason = reason or "fns_no_availability_enforced"
            map_image_url = ""
            maps_url = ""
            image_urls: list[str] = list(room_photo_urls or [])
            guest_blob = _guest_blob_from_messages(messages)
            distance_only = _looks_like_distance_question(guest_blob) or str(
                route_reply.get("style") if route_reply else ""
            ) == "eta"
            if action == "reply" and route_reply:
                language = _route_language(messages)
                if distance_only:
                    # Force car ETA only — never attach a map for “how far / how long”.
                    if str(route_reply.get("style") or "") != "eta":
                        try:
                            from app.services.hostel_directions import driving_eta_from_hostel

                            eta = driving_eta_from_hostel(
                                str(route_reply.get("destination") or "")
                            )
                            if isinstance(eta, dict) and eta.get("ok"):
                                route_reply = eta
                        except Exception:
                            pass
                    reply_message = _short_driving_eta_message(route_reply, language)
                    map_image_url = ""
                    maps_url = ""
                    reason = reason or "driving_eta"
                else:
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
            prior_assistant = [
                {"role": "assistant", "content": str(turn.get("content") or "")}
                for turn in messages
                if turn.get("role") == "assistant"
            ]
            already_wa = thread_already_shared_whatsapp(prior_assistant)
            reply_message = strip_unsolicited_goodbye(reply_message, guest_text=guest_blob)
            reply_message = sanitize_outbound_message(reply_message)
            reply_message = strip_repeated_whatsapp_number(
                reply_message, already_shared=already_wa
            )
            reply_message = enforce_single_short_reply(reply_message)
            if action == "reply" and not reply_message:
                action = "escalate"
                reason = reason or "empty_reply"
            # Room photos take priority over map for this turn.
            if image_urls:
                map_image_url = ""
                maps_url = ""
                reason = reason or "room_photos"
                # Avoid WhatsApp dump when we are sending photos.
                reply_message = re.sub(
                    r"https?://(?:api\.)?blackcathostal\.com/api/w/\S*",
                    "",
                    reply_message or "",
                    flags=re.I,
                ).strip()
                if not reply_message:
                    reply_message = "Te mando las fotos."
            return {
                "action": action,
                "message": reply_message,
                "reason": reason,
                "model": model,
                "map_image_url": map_image_url,
                "maps_url": maps_url,
                "image_urls": image_urls,
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
    public_ugc: bool = False,
    channel: str = "instagram",
) -> dict[str, Any]:
    """Return {action, message, reason, model} using DeepSeek + knowledge tools."""
    set_reply_context(username=author_username, channel=channel)
    can_book = booking_allowed(channel=channel, username=author_username)
    prior = [
        turn
        for turn in (thread_history or [])
        if (turn.get("content") or "").strip() and turn.get("role") in {"user", "assistant"}
    ]
    is_follow_up = bool(prior)
    # On foreign/collab reels, prices/booking are redirected to DM by the caller.
    # Always quote our FAQ CLP rates — never Motor/USD — even for booking pilot users.
    if _looks_like_price_question(inbound_text) and not public_ugc:
        return _price_summary_reply(
            inbound_text=inbound_text,
            is_follow_up=is_follow_up,
            prior=prior,
        )
    if _looks_like_greeting_only(inbound_text):
        return _human_greeting_reply(
            inbound_text=inbound_text,
            is_follow_up=is_follow_up,
            prior=prior,
            skip_duplicate_greeting=skip_duplicate_greeting,
        )
    if _looks_like_goodbye_only(inbound_text):
        return _goodbye_reply(inbound_text=inbound_text, prior=prior)
    if is_follow_up and _looks_like_short_ack(inbound_text):
        # Avoid a second bubble like “Exacto, ese es el valor” after already answering.
        return _skip_reply("short_ack")
    if _looks_like_volunteer(inbound_text) and not public_ugc:
        return _volunteer_reply(inbound_text=inbound_text, prior=prior)
    if _looks_like_doc_id_question(inbound_text) and not public_ugc:
        return _unknown_whatsapp_reply(
            db,
            inbound_text=inbound_text,
            is_follow_up=is_follow_up,
            prior=prior,
            reason="doc_id_whatsapp",
        )
    # Pilot: allowlisted guests go through FNS Motor tools instead of WhatsApp-only.
    if _looks_like_reservation(inbound_text) and not public_ugc and not can_book:
        return _reservation_whatsapp_reply(
            db,
            inbound_text=inbound_text,
            is_follow_up=is_follow_up,
            prior=prior,
        )
    distance_hint = (
        "\n\nEste mensaje pregunta tiempo/distancia: usa get_directions_from_hostel "
        "con eta_only=true y responde solo minutos en auto, sin mapa."
        if _looks_like_distance_question(inbound_text) and not public_ugc
        else ""
    )
    ugc_hint = (
        "\n\nComentario público en reel ajeno/colaborativo: responde corto, cálido y "
        "ejecutivo; dialoga con la gente (ej. «qué lindo» → agradece y sigue el hilo). "
        "NUNCA publiques precios, tarifas, WhatsApp ni datos de reserva aquí."
        if public_ugc
        else ""
    )
    booking_hint = (
        "\n\nEste huésped SÍ puede reservar por API FNSrooms. "
        "Si pide reserva/disponibilidad: pide fechas, personas, nombre, apellido "
        "y RUT o número de pasaporte. "
        "Pasaporte: pide número de pasaporte + nombre completo. "
        "RUT: basta el número. "
        "REGLA CLAVE: en cuanto el huésped dé identidad, "
        "ANTES de create_room_booking llama SIEMPRE check_guest_bookings "
        "(con esos datos). No crees sin haber buscado si ya tiene reserva sin usar. "
        "Si has_active_booking=true: dile fechas/nombre y pregunta "
        "«¿otra, eliminar esa, o cambiarla?» — no crees en silencio. "
        "Si NO recuerda el documento: nombre y apellido → check_guest_bookings → "
        "muéstrale booking_summaries "
        "(«encontré reserva a nombre de X del día Y al Z, ¿es esa?») → "
        "solo si dice sí, cancel/change con guest_confirmed=true. "
        "Orden tipico: identidad → check_guest_bookings → "
        "search_room_availability → create_room_booking. "
        "Eliminar → cancel_room_booking. "
        "Cambiar → change_room_booking (OBLIGATORIO: cancela vieja + crea nueva). "
        "REGLA ANTI-INVENCIÓN (crítica): "
        "NUNCA inventes disponibilidad ni digas «sí hay después/otras fechas» "
        "sin search_room_availability para ESAS fechas. "
        "Si availability_summary dice NO HAY CUPO / available=false / rooms=[]: "
        "di que no hay cupo. No lo contradigas en el mismo hilo. "
        "Si el huésped propone otras fechas, llama search_room_availability otra vez "
        "antes de afirmar cupo. "
        "Solo di 'hay cupo' si el ÚLTIMO search_room_availability devolvió rooms "
        "con inventory>0 o shared_available=true / HAY CUPO. "
        "Si shared_available=true: di que SÍ hay compartida y ANTES de reservar di exactamente: "
        "«Tenemos compartidas solo para hombres y solo para mujeres; ¿cuál requieres?». "
        "NO preguntes orientación sexual ni fuerces «¿eres hombre o mujer?». "
        "Cuando elija, create/change con shared_dorm_preference=men|women y código 22851/22853. "
        "Si quiere CAMBIAR o EXTENDER: primero search_room_availability de las fechas "
        "nuevas; solo si hay cupo, change_room_booking. "
        "Extender: recalcula noches x guest_price_per_night_clp del CRUD SOLO si FNS confirma cupo. "
        "NUNCA para 'cambiar' uses create_room_booking con confirm_extra_booking; "
        "eso deja la vieja viva. Si usas create para cambiar: replace_existing=true. "
        "Otra adicional (mantener la vieja) → confirm_extra_booking=true solo si insiste. "
        "Al cotizar da SOLO CLP de search_rooms / guest_price_*_clp del último availability "
        "(CRUD). No inventes montos. "
        "No digas USD. "
        "Para create/change GUARDA en CLP: currency=CLP, "
        "amount_after_tax=booking_amount_after_tax, "
        "day_rates_json=booking_day_rates_json, guest_document=RUT/pasaporte. "
        "Confirma habitación y total CLP antes de crear. No inventes códigos."
        if can_book and not public_ugc
        else ""
    )
    base, model, headers = _require_deepseek()
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": system_prompt_for_reply(
                distance_hint + ugc_hint + booking_hint, is_follow_up=is_follow_up
            ),
        },
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
                enforce_fns_availability=bool(can_book and not public_ugc),
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
    if _looks_like_price_question(inbound_text):
        result = _price_summary_reply(
            inbound_text=inbound_text,
            is_follow_up=is_follow_up,
            prior=prior,
        )
        result["channel"] = channel
        return result
    if _looks_like_greeting_only(inbound_text):
        result = _human_greeting_reply(
            inbound_text=inbound_text,
            is_follow_up=is_follow_up,
            prior=prior,
        )
        result["channel"] = channel
        return result
    if _looks_like_goodbye_only(inbound_text):
        result = _goodbye_reply(inbound_text=inbound_text, prior=prior)
        result["channel"] = channel
        return result
    if is_follow_up and _looks_like_short_ack(inbound_text):
        result = _skip_reply("short_ack")
        result["channel"] = channel
        return result
    if _looks_like_volunteer(inbound_text):
        result = _volunteer_reply(inbound_text=inbound_text, prior=prior)
        result["channel"] = channel
        return result
    if _looks_like_doc_id_question(inbound_text):
        result = _unknown_whatsapp_reply(
            db,
            inbound_text=inbound_text,
            is_follow_up=is_follow_up,
            prior=prior,
            reason="doc_id_whatsapp",
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
    extra = f"\n\nCanal de esta conversación de prueba: {channel_label}."
    if _looks_like_distance_question(inbound_text):
        extra += (
            "\nEste mensaje pregunta tiempo/distancia: get_directions_from_hostel "
            "eta_only=true; solo minutos en auto, sin mapa."
        )
    base, model, headers = _require_deepseek()
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": system_prompt_for_reply(extra, is_follow_up=is_follow_up),
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
