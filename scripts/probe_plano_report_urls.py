from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin, unquote

import httpx

from app.core.config import settings

BASE = (settings.facturacion_web_test_url or "https://www.facturacion.cl/plano/").rstrip("/") + "/"
USER = (settings.facturacion_web_test_user or settings.facturacion_user or "").strip()
PASSWORD = (settings.facturacion_web_test_clave or "").strip()


def login(client: httpx.Client) -> None:
    page = client.get(BASE)
    action_match = re.search(r'<form[^>]+action=["\']([^"\']*)["\']', page.text or "", re.I)
    action = urljoin(str(page.url), action_match.group(1) if action_match else "index.php")
    client.post(
        action,
        data={
            "user": USER,
            "pass": PASSWORD,
            "Submit": "Ingresar",
            "href": "",
            "urlRedirect": "",
        },
    )


def main() -> None:
    with httpx.Client(timeout=90.0, follow_redirects=True) as client:
        login(client)
        html = client.get(urljoin(BASE, "form/contabilidad/libros2/?indice=2&fromMenu=true")).text or ""

        # Extract report URLs fully
        for match in re.finditer(r"reporte\.php\?[^\"'\\]+", html, re.I):
            print("REPORT", unquote(match.group(0))[:300])

        # Find hash/token par4
        tokens = re.findall(r"par4=([a-f0-9]+)", html, re.I)
        print("tokens", tokens[:5])

        # Try excel-like report names with current token
        token = tokens[0] if tokens else ""
        names = [
            "reporte_librocompras2",
            "reporte_librocomprascompleto",
            "reporte_librocompras_excel",
            "reporte_librocompras2_excel",
            "excel_librocompras",
            "librocompras_excel",
            "reporte_librocomprasxls",
            "reporte_librocomprasxlsx",
        ]
        for name in names:
            url = (
                f"{BASE}rep/reporte.php?sistema=plano&reporte={name}"
                f"&modo=1&par1=2025&par2=11&par3={USER}&par4={token}"
            )
            response = client.get(url)
            print(
                name,
                response.status_code,
                response.headers.get("content-type"),
                response.headers.get("content-disposition"),
                len(response.content),
                response.content[:80],
            )

        # Inspect grid table header/body structure
        grid = re.search(r'class=["\']LIBROS_GRID["\'][\s\S]{0,8000}', html, re.I)
        if grid:
            Path("uploads/cache/plano_compras_grid_snip.html").write_text(
                grid.group(0), encoding="utf-8", errors="ignore"
            )
            print("grid snip saved, has tbody?", "<tbody" in grid.group(0).lower())
            print(" ".join(grid.group(0).split())[:1200])

        # Search JS for GRID_Action excel assignment pattern in inline scripts
        for match in re.finditer(r".{0,80}GRID_Action.{0,120}", html, re.I | re.S):
            print("GA", " ".join(match.group(0).split())[:250])


if __name__ == "__main__":
    main()
