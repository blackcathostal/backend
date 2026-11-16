from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

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


def extract_hidden(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in re.finditer(
        r'<input[^>]+type=["\']hidden["\'][^>]*>',
        html,
        re.I,
    ):
        tag = match.group(0)
        name = re.search(r'name=["\']([^"\']+)["\']', tag, re.I)
        value = re.search(r'value=["\']([^"\']*)["\']', tag, re.I)
        if name:
            out[name.group(1)] = value.group(1) if value else ""
    return out


def main() -> None:
    today = date.today()
    year = str(today.year)
    month = f"{today.month:02d}"
    # Also try previous month in case current is empty
    if today.month == 1:
        year2, month2 = str(today.year - 1), "12"
    else:
        year2, month2 = str(today.year), f"{today.month - 1:02d}"

    with httpx.Client(timeout=90.0, follow_redirects=True) as client:
        login(client)
        form_page = client.get(urljoin(BASE, "form/contabilidad/libros2/?indice=2&fromMenu=true"))
        html = form_page.text or ""
        Path("uploads/cache/plano_compras_form.html").write_text(html, encoding="utf-8", errors="ignore")

        # Find excel/report URLs embedded in JS
        urls = re.findall(r"https?://[^\"'\\s]+", html)
        interesting = [
            u
            for u in urls
            if any(k in u.lower() for k in ("excel", "reporte", "librocompra", "xls", "csv"))
        ]
        print("embedded urls", interesting[:30])

        excel_hits = [
            " ".join(m.group(0).split())[:400]
            for m in re.finditer(r".{0,80}(excel|xlsx|xls|csv).{0,120}", html, re.I | re.S)
        ]
        print("excel hits", len(excel_hits))
        for hit in excel_hits[:40]:
            print(" ", hit)

        hidden = extract_hidden(html)
        print("hidden keys", sorted(hidden.keys())[:40])

        for label, y, m in (("current", year, month), ("prev", year2, month2)):
            payload = {
                **hidden,
                "anno": y,
                "mes": m,
                "rut": "",
                "folio": "",
                "razonsocial": "",
                "sucursal": "",
                "xml": "",
                "tipocompra": "",
                "pagado": "",
                "estado": "",
                "tipoingreso": "",
                "tipoinformacion": "1",
                "montototal": "",
                "fecha1": "",
                "fecha2": "",
                "fechaven1": "",
                "fechaven2": "",
                "tipodocumentosseleccionados": "",
                "asignacion_bodega": "",
                "asignacion_centrocosto": "",
                "GRID_NumReg": "200",
                "GRID_NumRegAux": "200",
                "GRID_segundaCarga": "true",
                "GRID_OrderBy": "",
            }
            response = client.post(
                urljoin(BASE, "form/contabilidad/libros2/index.php"),
                data=payload,
            )
            text = response.text or ""
            Path(f"uploads/cache/plano_compras_search_{label}_{y}_{m}.html").write_text(
                text, encoding="utf-8", errors="ignore"
            )
            print(label, y, m, "status", response.status_code, "len", len(text))

            # table rows
            rows = re.findall(r"<tr[^>]*>.*?</tr>", text, re.I | re.S)
            print("  tr count", len(rows))
            # look for folio-like cells
            sample_rows = []
            for row in rows:
                cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.I | re.S)
                clean = [" ".join(re.sub(r"<[^>]+>", "", c).split()) for c in cells]
                if any(clean) and len(clean) >= 4:
                    joined = " | ".join(clean)
                    if any(ch.isdigit() for ch in joined) and "Folio" not in joined:
                        sample_rows.append(joined[:250])
                if len(sample_rows) >= 8:
                    break
            for s in sample_rows:
                print("  ROW", s)

            # command.php getlibro / summary
            cmd = client.post(
                urljoin(BASE, "form/contabilidad/libros2/command.php"),
                data={"cmd": "getSummaryDataVen", "module": "COMPRA", "anno": y, "mes": m},
            )
            print("  summary", cmd.status_code, (cmd.text or "")[:250])

        # Try common excel report endpoints seen in similar DESIS apps
        for path in interesting[:10]:
            try:
                r = client.get(path)
                ct = r.headers.get("content-type", "")
                print(
                    "GET report",
                    path[:120],
                    r.status_code,
                    ct,
                    "len",
                    len(r.content),
                    "start",
                    r.content[:20],
                )
            except Exception as exc:  # noqa: BLE001
                print("GET report fail", path[:80], exc)


if __name__ == "__main__":
    main()
