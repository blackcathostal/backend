from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urljoin

import httpx

from app.core.config import settings

OUT = Path("uploads/cache")


def acceso(client: httpx.Client, parameter: str) -> str:
    response = client.post(
        "https://www.facturacion.cl/desis/accesoFE3.php",
        content=(parameter + "&commandfile=").encode("latin-1", errors="ignore"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=ISO-8859-1",
            "AXJAGENT": "LOGIN",
        },
    )
    return response.text or ""


def main() -> None:
    users = [
        "ADMIN",
        "ANGELICA",
        "JESUS",
        "JESÚS",
        "BLACKCAT",
        "CONTABILIDAD",
        "CONTADOR",
        "HOSTAL",
        "USUARIO",
        "ROOT",
        "DESIS",
    ]
    passwords = [
        settings.facturacion_web_test_clave,
        settings.facturacion_prod_clave,
        settings.facturacion_des_clave,
        "ANGELICA1",
        "Angelica1",
        "admin123",
        "Admin123",
        "ADMIN123",
        "BlackCat2026.",
        "blackcat",
        "Blackcat",
        "BLACKCAT",
        "77014657",
        "77014657-7",
        "Integracion.123",
        "plano91098",
        "1b794102e0",
    ]
    empresa = "BLACKCAT"

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        client.get("https://www.facturacion.cl/")
        for user in users:
            # first check if user exists with a dummy pass
            text = acceso(
                client,
                f"&cmd=post&empresa=+{empresa}&usuario={user}&password=__probe__",
            )
            err = re.search(r'error="([^"]*)"', text, re.I)
            err_val = err.group(1) if err else ""
            if "no existe" in err_val.lower():
                print("user missing", user)
                continue
            print("user candidate", user, "err_with_dummy=", err_val[:80])
            for password in passwords:
                if not password:
                    continue
                text = acceso(
                    client,
                    f"&cmd=post&empresa=+{empresa}&usuario={user.upper()}"
                    f"&password={password}",
                )
                web = re.search(r"<web[^>]*>([^<]*)</web>", text, re.I)
                err = re.search(r'error="([^"]*)"', text, re.I)
                if web and web.group(1).strip():
                    host = unquote(web.group(1).strip())
                    print("LOGIN OK", user, password[:3] + "***", "->", host)
                    response = client.post(
                        f"https://{host}/index.php",
                        data={"empresa": empresa, "user": user.upper(), "pass": password},
                    )
                    html = response.text or ""
                    OUT.joinpath("blackcat_logged_in.html").write_text(
                        html, encoding="utf-8", errors="ignore"
                    )
                    print(
                        " tenant",
                        response.url,
                        "len",
                        len(html),
                        "has Libro",
                        "Libro" in html,
                    )
                    # open compras
                    compras = client.get(
                        urljoin(str(response.url), "form/contabilidad/libros2/?indice=2&fromMenu=true")
                    )
                    OUT.joinpath("blackcat_compras.html").write_text(
                        compras.text or "", encoding="utf-8", errors="ignore"
                    )
                    print(
                        " compras",
                        compras.status_code,
                        len(compras.text or ""),
                        "empty",
                        "No se encontraron resultados" in (compras.text or ""),
                    )
                    return
                err_val = err.group(1) if err else ""
                if "incorrecta" not in err_val.lower() and "no existe" not in err_val.lower():
                    print("  odd", user, password[:3], err_val[:100])
        print("NO LOGIN")


if __name__ == "__main__":
    main()
