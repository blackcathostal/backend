from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin

import httpx

from app.core.config import settings

BASE = (settings.facturacion_web_test_url or "https://www.facturacion.cl/plano/").rstrip("/") + "/"


def login(client: httpx.Client, user: str, password: str) -> bool:
    page = client.get(BASE)
    action_match = re.search(r'<form[^>]+action=["\']([^"\']*)["\']', page.text or "", re.I)
    action = urljoin(str(page.url), action_match.group(1) if action_match else "index.php")
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
    ok = "Libro Compras" in (response.text or "") or "CERRAR" in (response.text or "")
    print("login", user, ok, str(response.url), "cookies", list(client.cookies.keys()))
    return ok


def all_fields(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", html, re.I):
        name = re.search(r'name=["\']([^"\']+)["\']', tag, re.I)
        if not name:
            continue
        typ = re.search(r'type=["\']([^"\']+)["\']', tag, re.I)
        if typ and typ.group(1).lower() in {"submit", "button", "image", "file"}:
            continue
        value = re.search(r'value=["\']([^"\']*)["\']', tag, re.I)
        fields[name.group(1)] = value.group(1) if value else ""
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


def search(client: httpx.Client, *, year: str, month: str, label: str) -> None:
    form_html = client.get(urljoin(BASE, "form/contabilidad/libros2/?indice=2&fromMenu=true")).text or ""
    fields = all_fields(form_html)
    fields.update(
        {
            "indice": "2",
            "anno": year,
            "mes": month,
            "tipoinformacion": "1",
            "GRID_NumReg": "500",
            "GRID_NumRegAux": "500",
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
    Path(f"uploads/cache/plano_compras_{label}.html").write_text(text, encoding="utf-8", errors="ignore")

    # pagination text
    page_info = re.search(r"P.?gina\s+(\d+)\s+de\s+(\d+)", text, re.I)
    docs = re.search(r"(\d+)\s+documento", text, re.I)
    print(
        label,
        year,
        month,
        "len",
        len(text),
        "page",
        page_info.group(0) if page_info else "-",
        "docs",
        docs.group(0) if docs else "-",
    )

    # Excel onclick after search
    excel = re.search(r"title=['\"]Exportar a Excel['\"][^>]*>", text, re.I)
    if excel:
        block = text[max(0, excel.start() - 50) : excel.end() + 350]
        print(" excel onclick:", " ".join(block.split())[:450])

    # Try excel action on same fields
    fields["GRID_Action"] = "excel"
    excel_resp = client.post(
        urljoin(BASE, "form/contabilidad/libros2/index.php"),
        data=fields,
    )
    ct = excel_resp.headers.get("content-type", "")
    cd = excel_resp.headers.get("content-disposition", "")
    print(
        " excel post",
        excel_resp.status_code,
        ct,
        cd,
        "len",
        len(excel_resp.content),
        excel_resp.content[:60],
    )

    # command getlibro / summary for COMPRA
    for cmd in (
        f"cmd=getSummaryData&module=COMPRA&anno={year}&mes={month}",
        f"cmd=getSummaryDataVen&module=COMPRA&anno={year}&mes={month}",
        f"cmd=getlibro&module=COMPRA&anno={year}&mes={month}",
    ):
        cmd_resp = client.post(
            urljoin(BASE, "form/contabilidad/libros2/command.php"),
            content=cmd,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        print(" cmd", cmd[:50], cmd_resp.status_code, (cmd_resp.text or "")[:220])


def main() -> None:
    trials = [
        (
            settings.facturacion_web_test_user or "BLACKCAT",
            settings.facturacion_web_test_clave or "",
            "web",
        ),
        (
            settings.facturacion_user,
            settings.facturacion_prod_clave,
            "prodclave",
        ),
        (
            settings.facturacion_user,
            settings.facturacion_des_clave,
            "desclave",
        ),
    ]
    periods = [
        ("2025", "11"),
        ("2025", "10"),
        ("2024", "12"),
        ("2026", "10"),
        ("2026", "11"),
    ]
    for user, password, tag in trials:
        if not user or not password:
            continue
        with httpx.Client(timeout=90.0, follow_redirects=True) as client:
            if not login(client, user, password):
                continue
            for year, month in periods:
                search(client, year=year, month=month, label=f"{tag}_{year}_{month}")


if __name__ == "__main__":
    main()
