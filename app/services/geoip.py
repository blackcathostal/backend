from __future__ import annotations

import hashlib
import logging
import re
from functools import lru_cache

import httpx

logger = logging.getLogger(__name__)

_TIMEZONE_COUNTRY = {
    "America/Santiago": ("CL", "Chile"),
    "America/Punta_Arenas": ("CL", "Chile"),
    "Pacific/Easter": ("CL", "Chile"),
    "America/Sao_Paulo": ("BR", "Brasil"),
    "America/Fortaleza": ("BR", "Brasil"),
    "America/Argentina/Buenos_Aires": ("AR", "Argentina"),
    "America/Lima": ("PE", "Perú"),
    "America/Bogota": ("CO", "Colombia"),
    "America/Mexico_City": ("MX", "México"),
    "America/New_York": ("US", "Estados Unidos"),
    "America/Chicago": ("US", "Estados Unidos"),
    "America/Denver": ("US", "Estados Unidos"),
    "America/Los_Angeles": ("US", "Estados Unidos"),
    "America/Toronto": ("CA", "Canadá"),
    "Europe/Madrid": ("ES", "España"),
    "Atlantic/Canary": ("ES", "España"),
    "Europe/Lisbon": ("PT", "Portugal"),
    "Europe/London": ("GB", "Reino Unido"),
    "Europe/Paris": ("FR", "Francia"),
    "Europe/Berlin": ("DE", "Alemania"),
    "Europe/Rome": ("IT", "Italia"),
    "Europe/Amsterdam": ("NL", "Países Bajos"),
    "Asia/Tokyo": ("JP", "Japón"),
    "Asia/Seoul": ("KR", "Corea del Sur"),
    "Asia/Shanghai": ("CN", "China"),
    "Australia/Sydney": ("AU", "Australia"),
    "Pacific/Auckland": ("NZ", "Nueva Zelanda"),
}

_PRIVATE = re.compile(
    r"^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[0-1])\.|::1|localhost|0\.0\.0\.0)",
    re.I,
)


def hash_ip(ip: str) -> str:
    value = (ip or "").strip()
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def device_from_ua(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if any(token in ua for token in ("ipad", "tablet")):
        return "tablet"
    if any(token in ua for token in ("mobile", "iphone", "android")):
        return "móvil"
    return "escritorio"


def browser_from_ua(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "edg/" in ua:
        return "Edge"
    if "chrome" in ua and "safari" in ua:
        return "Chrome"
    if "safari" in ua:
        return "Safari"
    if "firefox" in ua:
        return "Firefox"
    if "opera" in ua or "opr/" in ua:
        return "Opera"
    return "Otro"


def is_bot(user_agent: str) -> bool:
    ua = (user_agent or "").lower()
    hints = (
        "bot",
        "crawl",
        "spider",
        "slurp",
        "bingpreview",
        "facebookexternalhit",
        "preview",
        "lighthouse",
        "headless",
        "puppeteer",
        "prerender",
    )
    return any(token in ua for token in hints)


@lru_cache(maxsize=4096)
def lookup_geo(ip: str, timezone: str = "") -> dict[str, str]:
    fallback = _from_timezone(timezone)
    if not ip or _PRIVATE.match(ip):
        if fallback["country_code"]:
            return fallback
        return {
            "country_code": "LO",
            "country_name": "Local",
            "region": "",
            "city": "",
        }
    try:
        with httpx.Client(timeout=1.6) as client:
            response = client.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,country,countryCode,regionName,city"},
            )
        data = response.json()
        if data.get("status") == "success" and data.get("countryCode"):
            return {
                "country_code": str(data.get("countryCode") or "")[:8],
                "country_name": str(data.get("country") or "")[:80],
                "region": str(data.get("regionName") or "")[:80],
                "city": str(data.get("city") or "")[:80],
            }
    except Exception:
        logger.debug("GeoIP lookup failed for %s", ip[:16], exc_info=True)
    return fallback if fallback["country_code"] else {
        "country_code": "",
        "country_name": "Desconocido",
        "region": "",
        "city": "",
    }


def _from_timezone(timezone: str) -> dict[str, str]:
    code, name = _TIMEZONE_COUNTRY.get((timezone or "").strip(), ("", ""))
    return {
        "country_code": code,
        "country_name": name or ("Desconocido" if not code else name),
        "region": "",
        "city": "",
    }
