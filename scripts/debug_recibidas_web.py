from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import httpx

from app.core.config import settings
from app.services.facturacion_web import FacturacionWebClient

OUT = Path("uploads/cache")
OUT.mkdir(parents=True, exist_ok=True)


def try_login(user: str, password: str, label: str) -> httpx.Client | None:
    base = (settings.facturacion_web_test_url or "https://www.facturacion.cl/plano/").rstrip("/") + "/"
    client = httpx.Client(timeout=90.0, follow_redirects=True)
    page = client.get(base)
    action_match = re.search(r'<form[^>]+action=(["\'])([^"\']*)\1', page.text or "", re.I)
    action = urljoin(str(page.url), action_match.group(2) if action_match else "index.php")
    response = client.post(
        action,
        data={
            "user": user,
            "pass": password,
            "Submit": "Ingresar",
            "href": "",
            "urlRedirect": "",
        },
    )
    text = response.text or ""
    ok = "Libro Compras" in text or "CERRAR" in text
    # company hints
    ruts = re.findall(r"\b\d{1,2}\.?\d{3}\.?\d{3}-[\dkK]\b", text)
    print(
        label,
        "ok",
        ok,
        "url",
        response.url,
        "len",
        len(text),
        "sample_ruts",
        ruts[:5],
        "has 77014657",
        "77014657" in text.replace(".", ""),
    )
    OUT.joinpath(f"login_{label}.html").write_text(text, encoding="utf-8", errors="ignore")
    if not ok:
        client.close()
        return None
    return client


def search_month(client: httpx.Client, year: int, month: int, label: str) -> str:
    base = (settings.facturacion_web_test_url or "https://www.facturacion.cl/plano/").rstrip("/") + "/"
    form = client.get(urljoin(base, "form/contabilidad/libros2/?indice=2&fromMenu=true")).text or ""
    fields = FacturacionWebClient._extract_fields(form)
    fields.update(
        {
            "indice": "2",
            "anno": str(year),
            "mes": f"{month:02d}",
            "tipoinformacion": "1",
            "GRID_NumReg": "500",
            "GRID_NumRegAux": "500",
            "GRID_segundaCarga": "true",
            "GRID_Page": "1",
            "GRID_Action": "",
        }
    )
    # Also try empty period (all) and tipoinformacion=2
    response = client.post(urljoin(base, "form/contabilidad/libros2/index.php"), data=fields)
    html = response.text or ""
    OUT.joinpath(f"search_{label}_{year}_{month:02d}.html").write_text(
        html, encoding="utf-8", errors="ignore"
    )
    empty = "No se encontraron resultados" in html
    rows = re.findall(r"<tr[^>]*class=['\"]tr_lista_fila[^'\"]*['\"][^>]*>", html, re.I)
    folios = re.findall(r">\s*(\d{1,10})\s*<", html)
    print(
        " search",
        label,
        year,
        month,
        "empty",
        empty,
        "tr_lista_fila",
        len(rows),
        "numericish",
        len(folios),
        "len",
        len(html),
    )

    # alternate tipoinformacion=2 (periodo registro SII)
    fields["tipoinformacion"] = "2"
    response2 = client.post(urljoin(base, "form/contabilidad/libros2/index.php"), data=fields)
    html2 = response2.text or ""
    OUT.joinpath(f"search_{label}_{year}_{month:02d}_sii.html").write_text(
        html2, encoding="utf-8", errors="ignore"
    )
    print(
        "  tipoinformacion=2 empty",
        "No se encontraron resultados" in html2,
        "tr",
        len(re.findall(r"tr_lista_fila", html2, re.I)),
    )

    # empty anno/mes (no period filter)
    fields["anno"] = ""
    fields["mes"] = ""
    fields["tipoinformacion"] = "1"
    response3 = client.post(urljoin(base, "form/contabilidad/libros2/index.php"), data=fields)
    html3 = response3.text or ""
    OUT.joinpath(f"search_{label}_noperiod.html").write_text(html3, encoding="utf-8", errors="ignore")
    print(
        "  no period empty",
        "No se encontraron resultados" in html3,
        "tr",
        len(re.findall(r"tr_lista_fila", html3, re.I)),
    )
    return html


def main() -> None:
    trials = [
        ("web", settings.facturacion_web_test_user, settings.facturacion_web_test_clave),
        ("prodclave", settings.facturacion_user, settings.facturacion_prod_clave),
        ("desclave", settings.facturacion_user, settings.facturacion_des_clave),
        ("web_prodclave", settings.facturacion_web_test_user, settings.facturacion_prod_clave),
    ]
    periods = [(2026, 11), (2026, 10), (2026, 9), (2025, 11), (2025, 10)]
    for label, user, password in trials:
        if not user or not password:
            continue
        client = try_login(user, password, label)
        if not client:
            continue
        try:
            for year, month in periods:
                search_month(client, year, month, label)
        finally:
            client.close()

    # Also run current client fetch
    with FacturacionWebClient() as web:
        docs = web.fetch_recibidas(date(2026, 9, 1), date(2026, 11, 16))
    print("fetch_recibidas count", len(docs))


if __name__ == "__main__":
    main()
