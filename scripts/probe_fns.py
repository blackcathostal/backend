"""Probe FNSrooms Motor + PMS with credentials from .env."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode

import httpx

from app.core.config import settings


def _clip(text: str, n: int = 500) -> str:
    return " ".join((text or "").split())[:n]


async def try_req(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kw,
) -> dict:
    try:
        response = await client.request(method, url, timeout=30.0, **kw)
        return {
            "status": response.status_code,
            "url": str(response.url),
            "ct": response.headers.get("content-type", ""),
            "body": _clip(response.text),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": None, "url": url, "error": str(exc)}


async def main() -> None:
    motor = settings.fns_motor_base_url.rstrip("/")
    pms = settings.fns_pms_base_url.rstrip("/")
    channel = {
        "user": settings.fns_channel_user,
        "password": settings.fns_channel_password,
        "channel_id": settings.fns_channel_id,
    }
    pms_auth = {
        "user": settings.fns_pms_user,
        "password": settings.fns_pms_password,
        "pms_id": settings.fns_pms_id,
        "usuario": settings.fns_pms_user,
        "pass": settings.fns_pms_password,
    }
    hotels = [
        h
        for h in [
            settings.fns_hotel_id.strip() or None,
            settings.fns_pms_id,
            settings.fns_channel_id,
            "11",
            "106",
        ]
        if h
    ]
    # unique preserve order
    seen: set[str] = set()
    hotel_ids: list[str] = []
    for h in hotels:
        if h not in seen:
            seen.add(h)
            hotel_ids.append(h)

    motor_paths = [
        "/search_availability_json.php",
        "/ws/search_availability_json.php",
        "/api/search_availability_json.php",
        "/motor/search_availability_json.php",
        "/channel/search_availability_json.php",
        "/json/search_availability_json.php",
        "/booking_json.php",
        "/ws/booking_json.php",
        "/get_booking_json.php",
    ]
    dates = {
        "fecha_entrada": "2026-12-10",
        "fecha_salida": "2026-12-12",
        "adultos": "2",
        "ninos": "0",
        "idioma": "es",
        "checkin": "2026-12-10",
        "checkout": "2026-12-12",
        "adults": "2",
        "children": "0",
        "lang": "es",
    }

    async with httpx.AsyncClient(follow_redirects=True) as client:
        print("=== MOTOR ROOT ===")
        for path in ["/", "/api-doc/", "/api/", "/ws/", "/motor/"]:
            res = await try_req(client, "GET", motor + path)
            print(res.get("status"), path, res.get("error") or res.get("body", "")[:180])

        print("\n=== MOTOR AVAILABILITY ===")
        interesting = 0
        for hotel in hotel_ids:
            for path in motor_paths:
                data = {**channel, **dates, "hotel_id": hotel}
                for method, kw in (
                    ("POST", {"data": data}),
                    ("GET", {"params": data}),
                    ("POST", {"json": data}),
                ):
                    res = await try_req(client, method, motor + path, **kw)
                    status = res.get("status")
                    if status in (None, 404):
                        continue
                    interesting += 1
                    print(
                        f"{method} {status} hotel={hotel} {path} :: "
                        f"{res.get('error') or res.get('body')}"
                    )
                    if interesting >= 40:
                        break
                if interesting >= 40:
                    break
            if interesting >= 40:
                break
        if interesting == 0:
            print("No non-404 Motor responses on known paths.")

        print("\n=== PMS ROOT ===")
        for path in [
            "/",
            "/api-doc/",
            "/api-doc/pms/",
            "/api/",
            "/api/pms/",
            "/pms/",
            "/ws/",
        ]:
            res = await try_req(client, "GET", pms + path)
            print(res.get("status"), path, res.get("error") or res.get("body", "")[:200])

        print("\n=== PMS AUTH / MAESTROS ===")
        pms_paths = [
            "/api/pms/login",
            "/api/login",
            "/login",
            "/api/pms/auth",
            "/api/auth",
            "/maestros/alojamiento.php",
            "/api/pms/maestros/alojamiento.php",
            "/api/pms/maestros/alojamiento",
            "/pms/maestros/alojamiento.php",
            "/api/pms/disponibilidad",
            "/api/pms/reservas",
            "/api/pms/reportes/cobros",
        ]
        count = 0
        for path in pms_paths:
            for method, kw in (
                ("POST", {"data": pms_auth}),
                ("POST", {"json": pms_auth}),
                ("GET", {"params": pms_auth}),
            ):
                res = await try_req(client, method, pms + path, **kw)
                status = res.get("status")
                if status in (None, 404):
                    continue
                count += 1
                print(
                    f"{method} {status} {path} :: "
                    f"{res.get('error') or res.get('body')}"
                )
        if count == 0:
            print("No non-404 PMS auth/maestro responses.")

        # Alternate hosts sometimes used by FNS Chile
        print("\n=== ALT HOSTS ===")
        for base in [
            "https://ms.fnsmanager.com",
            "https://admin.fnsbooking.com",
            "https://adminprees1.fnsmanager.com",
            "https://adminchile1.fnsmanager.com",
        ]:
            res = await try_req(client, "GET", base.rstrip("/") + "/")
            print(res.get("status"), base, (res.get("error") or res.get("body", ""))[:160])


if __name__ == "__main__":
    asyncio.run(main())
