from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin

import httpx

from app.core.config import settings


def main() -> None:
    user = settings.facturacion_web_test_user or settings.facturacion_user
    password = settings.facturacion_web_test_clave
    empresa = settings.facturacion_prod_rut
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        home = client.get("https://www.facturacion.cl/")
        Path("uploads/cache/facturacion_home.html").write_text(
            home.text or "", encoding="utf-8", errors="ignore"
        )
        # Find login form action
        forms = re.findall(r"<form[^>]*>.*?</form>", home.text or "", re.I | re.S)
        for i, form in enumerate(forms):
            if "pass" in form.lower() and "user" in form.lower():
                print("FORM", i, " ".join(form.split())[:500])
                action = re.search(r'action=["\']([^"\']*)["\']', form, re.I)
                method = re.search(r'method=["\']([^"\']*)["\']', form, re.I)
                print(" action", action.group(1) if action else "", method.group(1) if method else "")

        # Try common login endpoints
        payloads = [
            {"empresa": empresa, "user": user, "pass": password},
            {"rutempresa": empresa, "user": user, "pass": password},
            {"empresa": empresa, "usuario": user, "clave": password},
            {"rut": empresa, "user": user, "pass": password},
        ]
        endpoints = [
            "https://www.facturacion.cl/",
            "https://www.facturacion.cl/index.php",
            "https://www.facturacion.cl/login.php",
            "https://www.facturacion.cl/ingresar.php",
            "https://www.facturacion.cl/acceso.php",
        ]
        for endpoint in endpoints:
            for payload in payloads:
                response = client.post(endpoint, data=payload)
                text = response.text or ""
                interesting = any(
                    k in text.lower()
                    for k in ("cerrar ses", "logout", "libro compra", "bienvenido", "panel")
                )
                err = any(
                    k in text.lower()
                    for k in ("incorrect", "inválid", "invalido", "error de acceso", "no existe")
                )
                if interesting or (response.url and "plano" in str(response.url)):
                    print(
                        "HIT",
                        endpoint,
                        payload.keys(),
                        response.status_code,
                        response.url,
                        "err",
                        err,
                        "cookies",
                        list(client.cookies.keys()),
                    )
                    Path("uploads/cache/facturacion_main_login_hit.html").write_text(
                        text, encoding="utf-8", errors="ignore"
                    )
                    return
        print("no successful main login with available credentials")
        print("cookies", list(client.cookies.keys()))


if __name__ == "__main__":
    main()
