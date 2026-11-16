"""Try FNS PMS web login and Motor endpoints carefully."""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from app.core.config import settings

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"


def clip(text: str, n: int = 300) -> str:
    return " ".join((text or "").split())[:n]


def main() -> None:
    base = settings.fns_pms_base_url.rstrip("/")
    with httpx.Client(follow_redirects=True, timeout=25.0, headers={"User-Agent": UA}) as client:
        home = client.get(f"{base}/")
        print("HOME", home.status_code, clip(home.text, 120))
        # extract validacion form
        m = re.search(
            r'<form[^>]*name="validacion"[^>]*>(.*?)</form>',
            home.text,
            flags=re.I | re.S,
        )
        if not m:
            print("No validacion form found")
        else:
            fields = re.findall(
                r'<input[^>]*name=["\']([^"\']+)["\'][^>]*>',
                m.group(1),
                flags=re.I,
            )
            print("LOGIN FIELDS:", fields)

        payload = {
            "login": settings.fns_pms_user,
            "rpassword": settings.fns_pms_password,
            "accion": "ComprobarUser",
            "width": "1920",
            "height": "1080",
        }
        # also try with pms_id variants
        variants = [
            payload,
            {**payload, "pms_id": settings.fns_pms_id},
            {**payload, "alojamiento_id": settings.fns_pms_id},
            {**payload, "channel_id": settings.fns_channel_id},
            {
                "login": settings.fns_channel_user,
                "rpassword": settings.fns_channel_password,
                "accion": "ComprobarUser",
                "width": "1920",
                "height": "1080",
            },
        ]
        for i, data in enumerate(variants):
            r = client.post(f"{base}/es/Entrada", data=data)
            logged_in = (
                "cerrar sesión" in r.text.lower()
                or "logout" in r.text.lower()
                or "mi cuenta" in r.text.lower()
                or 'name="usuario_id" value="' in r.text
                and not re.search(r'name="usuario_id" value=""', r.text)
            )
            # Better signals
            still_login = 'name="login"' in r.text and "ComprobarUser" in r.text
            usuario_id = re.search(r'name=[\'"]usuario_id[\'"]\s+value=[\'"]([^\'"]*)[\'"]', r.text)
            error = re.search(r'name=[\'"]error[\'"]\s+value=[\'"]([^\'"]*)[\'"]', r.text)
            mensaje = re.search(r'name=[\'"]mensaje[\'"]\s+value=[\'"]([^\'"]*)[\'"]', r.text)
            print(
                f"TRY{i} status={r.status_code} still_login={still_login} "
                f"usuario_id={usuario_id.group(1) if usuario_id else None!r} "
                f"error={error.group(1) if error else None!r} "
                f"mensaje={mensaje.group(1) if mensaje else None!r} "
                f"keys={list(data.keys())}"
            )
            # save first response snippet markers
            if not still_login:
                Path("uploads/cache").mkdir(parents=True, exist_ok=True)
                Path(f"uploads/cache/fns_login_try{i}.html").write_text(r.text, encoding="utf-8")
                print("  saved uploads/cache/fns_login_try{i}.html")

        print("\n=== MOTOR via booking host ===")
        motor = settings.fns_motor_base_url.rstrip("/")
        channel_payload = {
            "user": settings.fns_channel_user,
            "password": settings.fns_channel_password,
            "channel_id": settings.fns_channel_id,
            "hotel_id": settings.fns_pms_id or settings.fns_hotel_id or settings.fns_channel_id,
            "fecha_entrada": "2026-12-10",
            "fecha_salida": "2026-12-12",
            "adultos": 2,
            "ninos": 0,
            "idioma": "es",
        }
        for path in [
            "/search_availability_json.php",
            "/ws/search_availability_json.php",
            "/channel/search_availability_json.php",
            "/json/search_availability_json.php",
        ]:
            try:
                r = client.post(motor + path, data=channel_payload)
                print(path, r.status_code, clip(r.text, 220))
            except Exception as exc:  # noqa: BLE001
                print(path, "ERR", exc)

        print("\n=== Direct availability with BasicAuth / headers ===")
        for auth in [
            (settings.fns_channel_user, settings.fns_channel_password),
            None,
        ]:
            try:
                r = client.post(
                    motor + "/search_availability_json.php",
                    data=channel_payload,
                    auth=auth,
                )
                print("auth", bool(auth), r.status_code, clip(r.text, 180))
            except Exception as exc:  # noqa: BLE001
                print("auth ERR", exc)


if __name__ == "__main__":
    main()
