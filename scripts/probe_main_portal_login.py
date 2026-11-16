from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlencode, urljoin

import httpx

from app.core.config import settings

OUT = Path("uploads/cache")
OUT.mkdir(parents=True, exist_ok=True)


def db_send(client: httpx.Client, parameter: str) -> str:
    body = (
        parameter
        + "&commandfile="
        + "&ipinicioconsultasesion=127.0.0.1"
        + "&sessionidinicioconsultasesion=probe"
    )
    # accesoFE3 embeds BASE with BD.url = /desis/accesoFE3.php
    response = client.post(
        "https://www.facturacion.cl/desis/accesoFE3.php",
        content=body.encode("latin-1", errors="ignore"),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=ISO-8859-1"},
    )
    return response.text or ""


def main() -> None:
    empresas = [
        settings.facturacion_prod_rut,
        "77014657-7",
        "BLACKCAT",
        "BLACK CAT",
        "blackcat",
    ]
    users = [
        settings.facturacion_web_test_user,
        settings.facturacion_user,
        "BLACKCAT",
        "ADMIN",
        "admin",
    ]
    passwords = [
        settings.facturacion_web_test_clave,
        settings.facturacion_prod_clave,
        settings.facturacion_des_clave,
    ]
    # unique non-empty
    seen = set()
    trials = []
    for empresa in empresas:
        for user in users:
            for password in passwords:
                if not empresa or not user or not password:
                    continue
                key = (empresa.upper(), user.upper(), password)
                if key in seen:
                    continue
                seen.add(key)
                trials.append((empresa, user, password))

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        client.get("https://www.facturacion.cl/")
        for empresa, user, password in trials:
            param = (
                "&cmd=post"
                f"&empresa=+{empresa}"
                f"&usuario={user}"
                f"&password={password}"
            )
            # also try without leading +
            for label, p in (
                ("plus", param),
                (
                    "plain",
                    f"&cmd=post&empresa={empresa}&usuario={user}&password={password}",
                ),
            ):
                text = db_send(client, p)
                OUT.joinpath(
                    f"acceso_{label}_{empresa}_{user}.xml".replace("/", "_").replace(" ", "_")
                ).write_text(text, encoding="utf-8", errors="ignore")
                web = re.search(r"<web[^>]*>(.*?)</web>", text, re.I | re.S)
                err = re.search(r'error="([^"]*)"', text, re.I) or re.search(
                    r"<error[^>]*>(.*?)</error>", text, re.I | re.S
                )
                web_val = web.group(1).strip() if web else ""
                err_val = (err.group(1) if err else "")[:120]
                print(
                    label,
                    empresa,
                    user,
                    "web=",
                    web_val,
                    "err=",
                    err_val,
                    "len",
                    len(text),
                    "snip",
                    " ".join(text.split())[:180],
                )
                if web_val:
                    # complete login to tenant
                    url = f"https://{web_val}/index.php"
                    response = client.post(
                        url,
                        data={
                            "empresa": empresa,
                            "user": user,
                            "pass": password,
                        },
                    )
                    html = response.text or ""
                    OUT.joinpath(f"tenant_{web_val.replace('.', '_')}.html").write_text(
                        html, encoding="utf-8", errors="ignore"
                    )
                    links = re.findall(
                        r'href=["\']([^"\']*(compra|libro|recib|dte)[^"\']*)["\']',
                        html,
                        re.I,
                    )
                    print(
                        "  TENANT",
                        response.url,
                        "len",
                        len(html),
                        "cookies",
                        list(client.cookies.keys())[:15],
                        "links",
                        [x[0] for x in links[:20]],
                    )
                    return


if __name__ == "__main__":
    main()
