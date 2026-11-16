from __future__ import annotations

import re
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


def all_fields(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", html, re.I):
        name = re.search(r'name=(["\'])([^"\']+)\1', tag, re.I)
        if not name:
            continue
        typ = re.search(r'type=(["\'])([^"\']+)\1', tag, re.I)
        if typ and typ.group(2).lower() in {"submit", "button", "image", "file"}:
            continue
        value = re.search(r'value=(["\'])([^"\']*)\1', tag, re.I)
        fields[name.group(2)] = value.group(2) if value else ""
    for match in re.finditer(
        r"<select[^>]*name=(['\"])([^'\"]+)\1[^>]*>(.*?)</select>",
        html,
        re.I | re.S,
    ):
        name = match.group(2)
        body = match.group(3)
        selected = re.search(
            r"<option[^>]*selected[^>]*value=(['\"])([^'\"]*)\1",
            body,
            re.I,
        )
        if selected:
            fields[name] = selected.group(2)
        else:
            first = re.search(r"<option[^>]*value=(['\"])([^'\"]*)\1", body, re.I)
            fields[name] = first.group(2) if first else ""
    return fields


def inspect(client: httpx.Client, indice: str, year: str, month: str, label: str) -> None:
    form_html = client.get(
        urljoin(BASE, f"form/contabilidad/libros2/?indice={indice}&fromMenu=true")
    ).text or ""
    fields = all_fields(form_html)
    fields.update(
        {
            "indice": indice,
            "anno": year,
            "mes": month,
            "tipoinformacion": "1",
            "GRID_NumReg": "100",
            "GRID_NumRegAux": "100",
            "GRID_segundaCarga": "true",
            "GRID_Page": "1",
            "GRID_Action": "",
        }
    )
    response = client.post(
        urljoin(BASE, "form/contabilidad/libros2/index.php"),
        data=fields,
    )
    text = response.text or ""
    Path(f"uploads/cache/plano_{label}.html").write_text(text, encoding="utf-8", errors="ignore")
    page_info = re.search(r"P.?gina.{0,40}", text, re.I | re.S)
    print(label, "len", len(text), "page", " ".join((page_info.group(0) if page_info else "-").split()))

    excel = re.search(
        r"title=['\"]Exportar a Excel['\"][\s\S]{0,500}?onClick=['\"](.*?)['\"]",
        text,
        re.I,
    )
    if excel:
        print(" excel onClick:", excel.group(1)[:500])
    else:
        # alternate attribute order
        excel2 = re.search(
            r"onClick=['\"](.*?)['\"][\s\S]{0,200}?title=['\"]Exportar a Excel['\"]",
            text,
            re.I,
        )
        print(" excel alt:", (excel2.group(1)[:500] if excel2 else "none"))

    # Look for document rows with folio numbers in grid
    if "No hay registros" in text:
        print("  empty notice present")
    # count numeric folios-ish in tbody
    tds = re.findall(r"<td[^>]*>(.*?)</td>", text, re.I | re.S)
    cleaned = [" ".join(re.sub(r"<[^>]+>", " ", t).split()) for t in tds]
    numericish = [c for c in cleaned if re.fullmatch(r"\d{1,10}", c or "")]
    print("  numeric cells sample", numericish[:20], "count", len(numericish))

    # Try excel export
    fields["GRID_Action"] = "excel"
    excel_resp = client.post(
        urljoin(BASE, "form/contabilidad/libros2/index.php"),
        data=fields,
    )
    print(
        "  excel",
        excel_resp.status_code,
        excel_resp.headers.get("content-type"),
        excel_resp.headers.get("content-disposition"),
        len(excel_resp.content),
        excel_resp.content[:50],
    )


def main() -> None:
    with httpx.Client(timeout=90.0, follow_redirects=True) as client:
        login(client)
        for indice, name in (("1", "ventas"), ("2", "compras")):
            for year, month in (("2026", "11"), ("2025", "11"), ("2024", "01")):
                inspect(client, indice, year, month, f"{name}_{year}_{month}")


if __name__ == "__main__":
    main()
