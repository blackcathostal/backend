from __future__ import annotations

import re
from urllib.parse import urljoin

import httpx

from app.core.config import settings


def main() -> None:
    web_user = (settings.facturacion_web_test_user or settings.facturacion_user or "").strip()
    web_pass = (settings.facturacion_web_test_clave or "").strip()
    base = (settings.facturacion_web_test_url or "https://www.facturacion.cl/plano/").rstrip("/") + "/"
    print("web_user", web_user, "base", base)

    with httpx.Client(timeout=45.0, follow_redirects=True) as client:
        # Main portal login (empresa/user/pass)
        home = client.get("https://www.facturacion.cl/")
        print("home", home.status_code, str(home.url))
        for payload in (
            {
                "empresa": settings.facturacion_prod_rut or "77014657-7",
                "user": web_user,
                "pass": web_pass,
            },
            {
                "empresa": "BLACKCAT",
                "user": web_user,
                "pass": web_pass,
            },
            {
                "empresa": settings.facturacion_user,
                "user": web_user,
                "pass": web_pass,
            },
        ):
            response = client.post("https://www.facturacion.cl/", data=payload)
            text = response.text or ""
            print(
                "main login",
                payload["empresa"],
                response.status_code,
                str(response.url),
                "len",
                len(text),
                "has error",
                "incorrect" in text.lower() or "inválid" in text.lower() or "invalido" in text.lower(),
            )
            # look for session cookies
            print(" cookies", list(client.cookies.keys())[:20])

        # Plano login
        plano = client.get(base)
        print("plano get", plano.status_code, str(plano.url))
        action_match = re.search(
            r'<form[^>]+action=["\']([^"\']*)["\']',
            plano.text or "",
            re.I,
        )
        action = urljoin(str(plano.url), action_match.group(1) if action_match else "")
        print("plano action", action)
        for user, password in (
            (web_user, web_pass),
            (settings.facturacion_user, settings.facturacion_des_clave),
            (settings.facturacion_user, settings.facturacion_prod_clave),
        ):
            response = client.post(
                action or base,
                data={
                    "user": user,
                    "pass": password,
                    "Submit": "Ingresar",
                    "href": "",
                    "urlRedirect": "",
                },
            )
            text = response.text or ""
            links = re.findall(r'href=["\']([^"\']*(compra|libro|contab|excel|export)[^"\']*)["\']', text, re.I)
            print(
                "plano login",
                user,
                response.status_code,
                str(response.url),
                "links",
                [x[0] for x in links[:15]],
                "title",
                (re.search(r"<title>(.*?)</title>", text, re.I | re.S) or [None, ""])[1][:80],
            )
            print(" cookies", list(client.cookies.keys())[:20])
            if "cerrar" in text.lower() or "logout" in text.lower() or links:
                # dump interesting anchors
                anchors = re.findall(r'href=["\']([^"\']+)["\'][^>]*>([^<]{0,60})', text, re.I)
                interesting = [
                    (href, label.strip())
                    for href, label in anchors
                    if any(
                        k in (href + " " + label).lower()
                        for k in (
                            "compra",
                            "libro",
                            "venta",
                            "contab",
                            "excel",
                            "export",
                            "recib",
                            "dte",
                        )
                    )
                ]
                print(" interesting", interesting[:40])
                Path_write = "uploads/cache/facturacion_plano_after_login.html"
                open(Path_write, "w", encoding="utf-8").write(text)
                print(" saved", Path_write)
                break


if __name__ == "__main__":
    main()
