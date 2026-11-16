from __future__ import annotations

import re
from pathlib import Path

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
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        client.get("https://www.facturacion.cl/")

        # Resolve system web host for empresa
        for empresa in (settings.facturacion_prod_rut, "BLACKCAT", "77014657-7"):
            text = acceso(client, f"&cmd=gsysname&empresa={empresa}")
            print("gsysname", empresa, "=>", " ".join(text.split())[:300])
            OUT.joinpath(f"gsysname_{empresa}.xml").write_text(text, encoding="utf-8", errors="ignore")

        passwords = [
            settings.facturacion_web_test_clave,
            settings.facturacion_prod_clave,
            settings.facturacion_des_clave,
        ]
        users = [settings.facturacion_web_test_user, settings.facturacion_user, "ADMIN"]
        empresas = [settings.facturacion_prod_rut, "BLACKCAT"]

        for empresa in empresas:
            for user in users:
                for password in passwords:
                    if not all([empresa, user, password]):
                        continue
                    param = (
                        "&cmd=post"
                        f"&empresa=+{empresa.upper()}"
                        f"&usuario={user.upper()}"
                        f"&password={password}"
                    )
                    text = acceso(client, param)
                    web = re.search(r"<web[^>]*>([^<]*)</web>", text, re.I)
                    err = re.search(r'error="([^"]*)"', text, re.I)
                    print(
                        "post",
                        empresa,
                        user,
                        password[:4] + "***",
                        "web=",
                        web.group(1) if web else "-",
                        "err=",
                        (err.group(1) if err else "-")[:100],
                        "snip",
                        " ".join(text.split())[:160],
                    )
                    if web and web.group(1).strip():
                        host = web.group(1).strip()
                        response = client.post(
                            f"https://{host}/index.php",
                            data={
                                "empresa": empresa.upper(),
                                "user": user.upper(),
                                "pass": password,
                            },
                        )
                        html = response.text or ""
                        OUT.joinpath(f"tenant_{host.replace('.', '_')}.html").write_text(
                            html, encoding="utf-8", errors="ignore"
                        )
                        print(
                            " TENANT OK",
                            host,
                            response.url,
                            "len",
                            len(html),
                            "cookies",
                            list(client.cookies.keys()),
                        )
                        # look for compras menu
                        for m in re.finditer(
                            r"refresca_formulario_Menu\(([^)]*)\).*{0,80}",
                            html,
                            re.I | re.S,
                        ):
                            ctx = html[m.start() : m.start() + 200]
                            if "ompr" in ctx.lower() or "libro" in ctx.lower():
                                print(" menu", m.group(1), " ".join(ctx.split())[:200])
                        return


if __name__ == "__main__":
    main()
