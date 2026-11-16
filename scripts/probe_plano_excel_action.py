from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin

import httpx

from app.core.config import settings

BASE = (settings.facturacion_web_test_url or "https://www.facturacion.cl/plano/").rstrip("/") + "/"
USER = (settings.facturacion_web_test_user or settings.facturacion_user or "").strip()
PASSWORD = (settings.facturacion_web_test_clave or "").strip()


def login(client: httpx.Client) -> str:
    page = client.get(BASE)
    action_match = re.search(r'<form[^>]+action=["\']([^"\']*)["\']', page.text or "", re.I)
    action = urljoin(str(page.url), action_match.group(1) if action_match else "index.php")
    response = client.post(
        action,
        data={
            "user": USER,
            "pass": PASSWORD,
            "Submit": "Ingresar",
            "href": "",
            "urlRedirect": "",
        },
    )
    return response.text or ""


def main() -> None:
    with httpx.Client(timeout=90.0, follow_redirects=True) as client:
        login(client)
        html = client.get(urljoin(BASE, "form/contabilidad/libros2/?indice=2&fromMenu=true")).text or ""

        # Excel onclick neighborhood
        for match in re.finditer(r".{0,200}Exportar a Excel.{0,400}", html, re.I | re.S):
            print("EXCEL BLOCK", " ".join(match.group(0).split())[:700])
            print("---")

        for match in re.finditer(r".{0,120}GRID_Action.{0,120}", html, re.I | re.S):
            print("GRID_Action", " ".join(match.group(0).split())[:300])

        # datagrid js
        for rel in (
            "componente/datagrid/class.php",
            "componente/datagrid/datagrid.js",
            "componente/datagrid/iu.js",
            "componente/database/class.php",
        ):
            response = client.get(urljoin(BASE, rel))
            text = response.text or ""
            Path(f"uploads/cache/plano_{rel.replace('/', '_')}").write_text(
                text, encoding="utf-8", errors="ignore"
            )
            print(rel, response.status_code, len(text))
            if "excel" in text.lower():
                for m in re.finditer(r".{0,80}excel.{0,120}", text, re.I | re.S):
                    print(" ", " ".join(m.group(0).split())[:280])

        # Look for LIBRO object excel methods in page scripts / linked js
        for rel in (
            "form/contabilidad/libros2/libro.js",
            "form/contabilidad/libros2/funciones.js",
            "form/contabilidad/libros2/index.js",
        ):
            response = client.get(urljoin(BASE, rel))
            print("js", rel, response.status_code, len(response.text or ""))

        # Extract script srcs under libros2
        srcs = re.findall(r'src=["\']([^"\']+)["\']', html, re.I)
        local = [s for s in srcs if "libros2" in s or "datagrid" in s or "database" in s]
        print("local scripts", local)
        for src in local:
            url = urljoin(urljoin(BASE, "form/contabilidad/libros2/"), src)
            response = client.get(url)
            text = response.text or ""
            print("fetch", url, response.status_code, len(text))
            if "excel" in text.lower() or "GRID_Action" in text:
                for m in re.finditer(
                    r".{0,100}(excel|GRID_Action|export).{0,160}",
                    text,
                    re.I | re.S,
                ):
                    print("  HIT", " ".join(m.group(0).split())[:320])

        # Try posting with GRID_Action=excel for compras
        payload = {
            "indice": "2",
            "anno": "2025",
            "mes": "11",
            "tipoinformacion": "1",
            "GRID_NumReg": "200",
            "GRID_NumRegAux": "200",
            "GRID_segundaCarga": "true",
            "GRID_Action": "excel",
            "GRID_Page": "1",
            "GRID_OrderBy": "",
            "GRID_OrderType": "",
        }
        for action in ("excel", "Excel", "EXCEL", "exportExcel", "toExcel"):
            payload["GRID_Action"] = action
            response = client.post(
                urljoin(BASE, "form/contabilidad/libros2/index.php"),
                data=payload,
            )
            ct = response.headers.get("content-type", "")
            cd = response.headers.get("content-disposition", "")
            print(
                "POST action",
                action,
                response.status_code,
                ct,
                cd,
                "len",
                len(response.content),
                response.content[:40],
            )


if __name__ == "__main__":
    main()
