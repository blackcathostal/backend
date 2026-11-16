from __future__ import annotations

import json
from datetime import date, timedelta

import httpx

from app.core.config import settings


def resolve_param(data: dict, param: dict) -> dict:
    ref = param.get("$ref")
    if not ref:
        return param
    name = ref.split("/")[-1]
    return ((data.get("components") or {}).get("parameters") or {}).get(name) or param


def main() -> None:
    schema = httpx.get(
        "https://adminprees1.fnsmanager.com/api-doc/pms/json/api-schema.json",
        timeout=30,
    ).json()
    interesting = [
        "/pms/v1/hotel/roomtypes/list.php",
        "/pms/v1/hotel/rooms/list.php",
        "/pms/v1/hotel/reservations/bookings.php",
        "/pms/v1/hotel/data/payments.php",
        "/pms/v1/hotel/ratesAndRestrictions/list.php",
    ]
    for path in interesting:
        print("====", path)
        get = ((schema.get("paths") or {}).get(path) or {}).get("get") or {}
        for raw in get.get("parameters") or []:
            p = resolve_param(schema, raw)
            print(
                f"  {p.get('name')} required={p.get('required')} in={p.get('in')} "
                f"{str(p.get('description') or '')[:90]}"
            )

    base = settings.fns_pms_base_url.rstrip("/")
    auth = (settings.fns_pms_user, settings.fns_pms_password)
    start = (date.today() - timedelta(days=7)).isoformat()
    end = date.today().isoformat()
    trials = [
        (
            "/pms/v1/hotel/roomtypes/list.php",
            {"pms_id": settings.fns_pms_id, "hotel_id": settings.fns_hotel_id},
        ),
        (
            "/pms/v1/hotel/rooms/list.php",
            {"pms_id": settings.fns_pms_id, "hotel_id": settings.fns_hotel_id},
        ),
        (
            "/pms/v1/hotel/reservations/bookings.php",
            {
                "pms_id": settings.fns_pms_id,
                "hotel_id": settings.fns_hotel_id,
                "start_date": start,
                "end_date": end,
            },
        ),
        (
            "/pms/v1/hotel/data/payments.php",
            {
                "pms_id": settings.fns_pms_id,
                "hotel_id": settings.fns_hotel_id,
                "start_date": start,
                "end_date": end,
            },
        ),
        (
            "/pms/v1/hotel/ratesAndRestrictions/list.php",
            {
                "pms_id": settings.fns_pms_id,
                "hotel_id": settings.fns_hotel_id,
                "start_date": start,
                "end_date": end,
            },
        ),
    ]
    print("\n=== CALLS ===")
    with httpx.Client(timeout=45.0, follow_redirects=True) as client:
        for path, params in trials:
            r = client.get(base + path, params=params, auth=auth)
            body = " ".join(r.text.split())[:260]
            print(r.status_code, path, params, "->", body)


if __name__ == "__main__":
    main()
