from __future__ import annotations

import json
from datetime import date, timedelta

import httpx

from app.core.config import settings


def resolve_param(schema: dict, param: dict) -> dict:
    ref = param.get("$ref")
    if not ref:
        return param
    name = ref.split("/")[-1]
    return ((schema.get("components") or {}).get("parameters") or {}).get(name) or param


def dump_endpoint(schema: dict, path: str) -> None:
    post = ((schema.get("paths") or {}).get(path) or {}).get("post") or {}
    print("====", path)
    print(" summary:", post.get("summary"))
    print(" tags:", post.get("tags"))
    print(" security:", post.get("security"))
    for raw in post.get("parameters") or []:
        p = resolve_param(schema, raw)
        desc = str(p.get("description") or "")[:140]
        print(
            f"  param {p.get('name')} in={p.get('in')} req={p.get('required')} "
            f"schema={p.get('schema')} desc={desc}"
        )
    rb = post.get("requestBody") or {}
    if rb:
        print(" requestBody:", json.dumps(rb, ensure_ascii=False)[:1200])
    for code, resp in (post.get("responses") or {}).items():
        print(" resp", code, str(resp.get("description") or "")[:120])
        content = resp.get("content") or {}
        for ctype, body in content.items():
            print("  ", ctype, json.dumps(body, ensure_ascii=False)[:700])


def try_calls() -> None:
    base = (settings.fns_ms_base_url or "https://ms.fnsmanager.com").rstrip("/")
    auth = (
        (settings.fns_ms_user or settings.fns_pms_user or "").strip(),
        (settings.fns_ms_password or settings.fns_pms_password or "").strip(),
    )
    start = (date.today().replace(day=1)).isoformat()
    end = date.today().isoformat()
    hotel = (settings.fns_hotel_pms_id or settings.fns_hotel_id or "").strip()
    pms_id = (settings.fns_pms_id or "").strip()
    central = (settings.fns_central_id or "").strip()

    bodies = [
        {
            "pms_id": pms_id,
            "hotel_id": hotel,
            "fecha_desde": start,
            "fecha_hasta": end,
        },
        {
            "pms_id": pms_id,
            "hotel_id": hotel,
            "desde": start,
            "hasta": end,
        },
        {
            "pms_id": pms_id,
            "alojamiento_id": hotel,
            "fecha_desde": start,
            "fecha_hasta": end,
        },
        {
            "central_id": central,
            "pms_id": pms_id,
            "hotel_id": hotel,
            "fecha_inicio": start,
            "fecha_fin": end,
        },
        {
            "auth": {"pms_id": pms_id},
            "params": {
                "hotel_id": hotel,
                "fecha_desde": start,
                "fecha_hasta": end,
            },
        },
    ]
    paths = [
        "/v1/contabilidad/facturacion.php",
        "/v1/contabilidad/cobros.php",
        "/v1/contabilidad/produccion.php",
    ]
    print("\n=== CALLS base", base, "auth_user", auth[0], "hotel", hotel, "pms", pms_id)
    with httpx.Client(timeout=45.0, follow_redirects=True, auth=auth) as client:
        for path in paths:
            for i, body in enumerate(bodies):
                try:
                    r = client.post(base + path, json=body)
                except Exception as exc:  # noqa: BLE001
                    print(path, "body", i, "EXC", exc)
                    continue
                text = " ".join((r.text or "").split())[:320]
                print(path, "body", i, r.status_code, text)
                if r.status_code < 400:
                    break


def main() -> None:
    schema = httpx.get(
        "https://ms.fnsmanager.com/api-doc/pms/json/api-schema.json",
        timeout=40,
    ).json()
    comps = schema.get("components") or {}
    print("=== securitySchemes ===")
    print(json.dumps(comps.get("securitySchemes") or {}, indent=2)[:1000])
    print("=== global security ===")
    print(schema.get("security"))
    for path in (
        "/v1/contabilidad/cobros.php",
        "/v1/contabilidad/facturacion.php",
        "/v1/contabilidad/produccion.php",
        "/v1/ventas/produccion.php",
    ):
        dump_endpoint(schema, path)
    try_calls()


if __name__ == "__main__":
    main()
