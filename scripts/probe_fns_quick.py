"""Quick FNS connectivity check with browser-like headers."""

from __future__ import annotations

import asyncio

import httpx

from app.core.config import settings

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/html, */*"}


def clip(text: str, n: int = 280) -> str:
    return " ".join((text or "").split())[:n]


async def hit(client: httpx.AsyncClient, method: str, url: str, **kw) -> None:
    try:
        r = await client.request(method, url, headers=HEADERS, timeout=12.0, **kw)
        print(f"{method} {r.status_code} {url}")
        print(" ", clip(r.text))
    except Exception as exc:  # noqa: BLE001
        print(f"{method} ERR {url} :: {exc}")


async def main() -> None:
    user = settings.fns_pms_user
    password = settings.fns_pms_password
    pms_id = settings.fns_pms_id
    channel_id = settings.fns_channel_id
    auth = {"user": user, "password": password, "pms_id": pms_id}
    channel = {
        "user": settings.fns_channel_user,
        "password": settings.fns_channel_password,
        "channel_id": channel_id,
        "hotel_id": pms_id,
        "fecha_entrada": "2026-12-10",
        "fecha_salida": "2026-12-12",
        "adultos": 2,
        "ninos": 0,
        "idioma": "es",
    }

    async with httpx.AsyncClient(follow_redirects=True) as client:
        print("=== HOSTS ===")
        for url in [
            "https://ms.fnsmanager.com/",
            "https://ms.fnsmanager.com/api-doc/pms/",
            "https://adminchile1.fnsmanager.com/",
            "https://admin.fnsbooking.com/",
            "https://adminchile1.fnsbooking.com/",
            "https://admin.fnsmanager.com/",
        ]:
            await hit(client, "GET", url)

        print("\n=== PMS LOGIN GUESSES ===")
        for base in ["https://ms.fnsmanager.com", "https://adminchile1.fnsmanager.com"]:
            for path in [
                "/api/pms/login",
                "/api/login",
                "/index.php",
                "/login.php",
                "/api/pms/maestros/alojamiento.php",
            ]:
                await hit(client, "POST", base + path, data=auth)
                await hit(client, "POST", base + path, json=auth)

        print("\n=== MOTOR GUESSES ===")
        for base in [
            "https://admin.fnsbooking.com",
            "https://adminchile1.fnsbooking.com",
            "https://adminchile1.fnsmanager.com",
        ]:
            for path in [
                "/search_availability_json.php",
                "/ws/search_availability_json.php",
                "/channel/search_availability_json.php",
            ]:
                await hit(client, "POST", base + path, data=channel)


if __name__ == "__main__":
    asyncio.run(main())
