"""Pull FNS swagger specs and retry Motor/PMS with documented paths."""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin

import httpx

from app.core.config import settings

UA = "Mozilla/5.0"


def clip(text: str, n: int = 240) -> str:
    return " ".join((text or "").split())[:n]


def main() -> None:
    auth_motor = (settings.fns_channel_user, settings.fns_channel_password)
    auth_pms = (settings.fns_pms_user, settings.fns_pms_password)
    with httpx.Client(timeout=45.0, follow_redirects=True, headers={"User-Agent": UA}) as client:
        print("=== SWAGGER PAGES ===")
        specs = []
        for base in [
            "https://adminprees1.fnsmanager.com/api-doc/channel/",
            "https://adminprees1.fnsmanager.com/api-doc/pms/",
        ]:
            r = client.get(base)
            print(r.status_code, base)
            refs = re.findall(r"""url:\s*["']([^"']+)["']""", r.text)
            refs += re.findall(r"""["']([^"']*swagger[^"']*\.json)["']""", r.text, flags=re.I)
            refs += re.findall(r"""["']([^"']*openapi[^"']*\.json)["']""", r.text, flags=re.I)
            for ref in sorted(set(refs)):
                url = urljoin(base, ref)
                print("  ref", url)
                try:
                    s = client.get(url)
                    print("   ", s.status_code, clip(s.text, 120))
                    if s.status_code < 400 and ("paths" in s.text or "swagger" in s.text.lower()):
                        specs.append((url, s.json() if s.headers.get("content-type", "").startswith("application/json") or s.text.strip().startswith("{") else None, s.text))
                except Exception as exc:  # noqa: BLE001
                    print("    ERR", exc)

        # brute common swagger filenames
        for url in [
            "https://adminprees1.fnsmanager.com/api-doc/channel/swagger.json",
            "https://adminprees1.fnsmanager.com/api-doc/pms/swagger.json",
            "https://adminprees1.fnsmanager.com/api-doc/channel/api-docs",
            "https://adminprees1.fnsmanager.com/api-doc/pms/api-docs",
            "https://adminprees1.fnsmanager.com/api-doc/channel/openapi.json",
            "https://adminprees1.fnsmanager.com/api-doc/pms/openapi.json",
        ]:
            try:
                s = client.get(url)
                if s.status_code < 400:
                    print("SPEC", s.status_code, url, clip(s.text, 100))
                    try:
                        specs.append((url, s.json(), s.text))
                    except Exception:
                        specs.append((url, None, s.text))
            except Exception as exc:  # noqa: BLE001
                print("ERR", url, exc)

        print("\n=== PATHS ===")
        for url, data, raw in specs:
            print("SPEC", url)
            if not isinstance(data, dict):
                # maybe yaml-ish html wrapper
                if "paths" in raw:
                    print(" raw has paths but not json")
                continue
            paths = list((data.get("paths") or {}).keys())
            print(" count", len(paths))
            for p in paths[:50]:
                methods = ",".join(sorted((data["paths"][p] or {}).keys()))
                print(f"  {methods:10} {p}")

        print("\n=== MOTOR exact Postman body ===")
        motor_url = (
            settings.fns_motor_base_url.rstrip("/")
            + settings.fns_motor_availability_path
        )
        body = {
            "auth": {"channel_id": "106"},
            "params": {
                "lang": "es",
                "hotel_id": "7402",
                "start_date": "2026-11-20",
                "end_date": "2026-11-21",
                "currency": "USD",
                "hotelData": 1,
            },
        }
        r = client.post(motor_url, json=body, auth=auth_motor)
        print(r.status_code, len(r.content))
        try:
            data = r.json()
            hotels = data.get("hotelAvailabilities")
            print("avail type", type(hotels).__name__, "len", len(hotels) if isinstance(hotels, list) else hotels)
            if isinstance(hotels, list) and hotels:
                h = hotels[0]
                print("hotel", h.get("hotel_id"), (h.get("hotelData") or {}).get("name"))
                # room-ish keys
                for k in h.keys():
                    if k.lower() not in {"hoteldata"}:
                        v = h.get(k)
                        if isinstance(v, list):
                            print(" ", k, "list", len(v))
                        elif isinstance(v, dict):
                            print(" ", k, "dict", list(v.keys())[:8])
                        else:
                            print(" ", k, v)
            else:
                print(clip(json.dumps(data, ensure_ascii=False), 400))
        except Exception as exc:  # noqa: BLE001
            print("parse", exc, clip(r.text))

        # If PMS paths discovered, hit a few on chile host
        print("\n=== PMS path trials on chile ===")
        pms_paths = []
        for url, data, raw in specs:
            if "pms" not in url.lower() or not isinstance(data, dict):
                continue
            pms_paths = list((data.get("paths") or {}).keys())
        base = settings.fns_pms_base_url.rstrip("/")
        for path in pms_paths[:20]:
            for method in ("GET", "POST"):
                try:
                    if method == "GET":
                        resp = client.request(
                            method,
                            base + path,
                            params={"pms_id": settings.fns_pms_id},
                            auth=auth_pms,
                        )
                    else:
                        resp = client.request(
                            method,
                            base + path,
                            json={"pms_id": settings.fns_pms_id},
                            auth=auth_pms,
                        )
                    if resp.status_code != 404:
                        print(method, resp.status_code, path, clip(resp.text, 180))
                except Exception as exc:  # noqa: BLE001
                    print(method, "ERR", path, exc)


if __name__ == "__main__":
    main()
