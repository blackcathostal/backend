"""Probe FNS Motor + PMS the way FNS documented (Basic Auth / API service)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx

from app.core.config import settings


def clip(obj, n: int = 500) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return " ".join(text.split())[:n]


def main() -> None:
    start = (date.today() + timedelta(days=7)).isoformat()
    end = (date.today() + timedelta(days=8)).isoformat()
    motor_url = (
        settings.fns_motor_base_url.rstrip("/")
        + settings.fns_motor_availability_path
    )
    motor_body = {
        "auth": {"channel_id": str(settings.fns_channel_id)},
        "params": {
            "lang": "es",
            "hotel_id": str(settings.fns_hotel_id),
            "start_date": start,
            "end_date": end,
            "currency": "CLP",
            "hotelData": 1,
        },
    }
    auth_motor = (settings.fns_channel_user, settings.fns_channel_password)
    auth_pms = (settings.fns_pms_user, settings.fns_pms_password)

    print("=== 1) MOTOR availability ===")
    print("URL", motor_url)
    print("hotel_id", settings.fns_hotel_id, "channel_id", settings.fns_channel_id)
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        r = client.post(motor_url, json=motor_body, auth=auth_motor)
        print("status", r.status_code, "bytes", len(r.content))
        try:
            data = r.json()
        except Exception:
            print("body", clip(r.text))
            data = None
        if isinstance(data, dict):
            hotels = data.get("hotelAvailabilities") or []
            print("hotels", len(hotels))
            if hotels:
                h0 = hotels[0]
                print(
                    "first",
                    {
                        "hotel_id": h0.get("hotel_id"),
                        "currency": h0.get("currency"),
                        "name": (h0.get("hotelData") or {}).get("name"),
                        "room_keys": list(h0.keys())[:12],
                    },
                )
            else:
                print("json", clip(data))

        print("\n=== 2) PMS API service (not portal) ===")
        pms = settings.fns_pms_base_url.rstrip("/")
        # Common FNS PMS patterns + a few doc-ish paths
        candidates = [
            ("GET", f"{pms}/api/pms/maestros/alojamiento", {"pms_id": settings.fns_pms_id}),
            ("GET", f"{pms}/api/pms/maestros/alojamiento.php", {"pms_id": settings.fns_pms_id}),
            ("POST", f"{pms}/api/pms/maestros/alojamiento", {"pms_id": settings.fns_pms_id}),
            ("GET", f"{pms}/api/pms/reservas", {"pms_id": settings.fns_pms_id}),
            ("POST", f"{pms}/api/pms/login", {"pms_id": settings.fns_pms_id}),
            ("GET", f"{pms}/api/pms/", {"pms_id": settings.fns_pms_id}),
            ("GET", f"{pms}/api/", {"pms_id": settings.fns_pms_id}),
        ]
        for method, url, params in candidates:
            try:
                if method == "GET":
                    resp = client.request(method, url, params=params, auth=auth_pms)
                else:
                    resp = client.request(
                        method,
                        url,
                        json={
                            "pms_id": settings.fns_pms_id,
                            "user": settings.fns_pms_user,
                            "password": settings.fns_pms_password,
                        },
                        auth=auth_pms,
                    )
                print(method, resp.status_code, url, clip(resp.text, 220))
            except Exception as exc:  # noqa: BLE001
                print(method, "ERR", url, exc)

        print("\n=== Docs host ===")
        for url in [
            "https://adminprees1.fnsmanager.com/api-doc/channel/",
            "https://adminprees1.fnsmanager.com/api-doc/pms/",
        ]:
            try:
                resp = client.get(url)
                print(resp.status_code, url, clip(resp.text, 160))
            except Exception as exc:  # noqa: BLE001
                print("ERR", url, exc)


if __name__ == "__main__":
    main()
