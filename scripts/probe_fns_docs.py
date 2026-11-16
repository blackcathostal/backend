"""Discover FNS PMS/Motor API docs and try authenticated calls."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin

import httpx

from app.core.config import settings


def clip(text: str, n: int = 400) -> str:
    return " ".join((text or "").split())[:n]


async def main() -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=40.0) as client:
        roots = [
            "https://ms.fnsmanager.com/",
            "https://ms.fnsmanager.com/api-doc/pms/",
            "https://ms.fnsmanager.com/api-doc/",
            "https://ms.fnsmanager.com/api-doc/pms/index.html",
            "https://ms.fnsmanager.com/lib/swagger/",
        ]
        print("=== DOCS PAGES ===")
        html_blobs: list[str] = []
        for url in roots:
            try:
                r = await client.get(url)
                print(r.status_code, url, r.headers.get("content-type"), clip(r.text, 180))
                if r.status_code < 400:
                    html_blobs.append(r.text)
                    for m in re.findall(
                        r"""['"]([^'"]*(?:swagger|openapi|api-docs|\.json)[^'"]*)['"]""",
                        r.text,
                        flags=re.I,
                    ):
                        print("  ref:", m)
            except Exception as exc:  # noqa: BLE001
                print("ERR", url, exc)

        candidates = set()
        for blob in html_blobs:
            for m in re.findall(
                r"""['"]([^'"]*(?:swagger|openapi|api-docs)[^'"]*)['"]""",
                blob,
                flags=re.I,
            ):
                if m.startswith("http"):
                    candidates.add(m)
                else:
                    candidates.add(urljoin("https://ms.fnsmanager.com/", m.lstrip("./")))
                    candidates.add(urljoin("https://ms.fnsmanager.com/api-doc/pms/", m))

        # Common swagger locations
        for extra in [
            "https://ms.fnsmanager.com/api-doc/pms/swagger.json",
            "https://ms.fnsmanager.com/api-doc/pms/openapi.json",
            "https://ms.fnsmanager.com/api-doc/swagger.json",
            "https://ms.fnsmanager.com/swagger/v1/swagger.json",
            "https://ms.fnsmanager.com/v2/api-docs",
            "https://ms.fnsmanager.com/api-doc/pms/api-docs",
            "https://ms.fnsmanager.com/api-doc/pms/swagger.yaml",
        ]:
            candidates.add(extra)

        print("\n=== SPEC CANDIDATES ===")
        specs = []
        for url in sorted(candidates):
            try:
                r = await client.get(url)
                print(r.status_code, url, clip(r.text, 120))
                if r.status_code < 400 and (
                    "paths" in r.text or "swagger" in r.text.lower() or "openapi" in r.text.lower()
                ):
                    specs.append((url, r.text))
            except Exception as exc:  # noqa: BLE001
                print("ERR", url, exc)

        # Try login against ms.fnsmanager.com with several known FNS styles
        print("\n=== AUTH TRIALS ON ms.fnsmanager.com ===")
        user = settings.fns_pms_user
        password = settings.fns_pms_password
        pms_id = settings.fns_pms_id
        channel_id = settings.fns_channel_id
        payloads = [
            {"user": user, "password": password, "pms_id": pms_id},
            {"usuario": user, "password": password, "pms_id": pms_id},
            {"username": user, "password": password, "pms_id": pms_id},
            {"user": user, "pass": password, "pms_id": pms_id},
            {"user": user, "password": password, "hotel_id": pms_id},
            {"user": user, "password": password, "channel_id": channel_id},
            {
                "user": settings.fns_channel_user,
                "password": settings.fns_channel_password,
                "channel_id": channel_id,
                "hotel_id": pms_id,
            },
        ]
        endpoints = [
            "/api/pms/login",
            "/api/login",
            "/login",
            "/api/pms/auth/login",
            "/api/auth/login",
            "/pms/login",
            "/api/token",
            "/oauth/token",
            "/api/pms/token",
        ]
        for base in [
            "https://ms.fnsmanager.com",
            "https://adminchile1.fnsmanager.com",
            "https://adminprees1.fnsmanager.com",
        ]:
            for path in endpoints:
                for payload in payloads[:3]:
                    for method, kw in (
                        ("POST", {"data": payload}),
                        ("POST", {"json": payload}),
                    ):
                        url = base.rstrip("/") + path
                        try:
                            r = await client.request(method, url, timeout=25.0, **kw)
                        except Exception as exc:  # noqa: BLE001
                            continue
                        if r.status_code in (404, 405):
                            continue
                        if r.status_code == 403 and "Forbidden" in r.text and len(r.text) < 400:
                            continue
                        print(
                            f"{method} {r.status_code} {url} payload_keys={list(payload)} :: {clip(r.text, 220)}"
                        )

        # If we got a swagger spec, print path keys
        print("\n=== SPEC PATHS (if any) ===")
        for url, text in specs[:2]:
            print("SPEC", url)
            try:
                import json

                data = json.loads(text)
                paths = list((data.get("paths") or {}).keys())[:40]
                print("paths:", paths)
                # auth security
                print("securityDefinitions/components:", clip(str(data.get("securityDefinitions") or data.get("components") or {}), 300))
            except Exception as exc:  # noqa: BLE001
                print("parse err", exc, clip(text, 200))


if __name__ == "__main__":
    asyncio.run(main())
