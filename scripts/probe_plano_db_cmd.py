from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin, urlencode

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


def fields_from(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", html, re.I):
        name = re.search(r'name=(["\'])([^"\']+)\1', tag, re.I)
        if not name:
            # id-only like sendFilter64
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
    # sendFilter64 often has id but may lack name
    sf = re.search(
        r'<input[^>]+id=["\']sendFilter64["\'][^>]*value=["\']([^"\']+)["\']',
        html,
        re.I,
    )
    if sf:
        fields["sendFilter64"] = sf.group(1)
    return fields


def db_send(client: httpx.Client, command_file: str, parameter: str) -> httpx.Response:
    body = (
        parameter
        + "&commandfile="
        + command_file
        + "&ipinicioconsultasesion=127.0.0.1"
        + "&sessionidinicioconsultasesion=blackcat"
    )
    return client.post(
        urljoin(BASE, "componente/database/class.php"),
        content=body.encode("latin-1", errors="ignore"),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=ISO-8859-1"},
    )


def main() -> None:
    with httpx.Client(timeout=90.0, follow_redirects=True) as client:
        login(client)
        form = client.get(urljoin(BASE, "form/contabilidad/libros2/?indice=2&fromMenu=true")).text or ""
        fields = fields_from(form)
        fields.update(
            {
                "indice": "2",
                "anno": "2025",
                "mes": "11",
                "tipoinformacion": "1",
                "GRID_NumReg": "200",
                "GRID_NumRegAux": "200",
                "GRID_segundaCarga": "true",
                "GRID_Page": "1",
                "GRID_Action": "",
            }
        )
        searched = client.post(
            urljoin(BASE, "form/contabilidad/libros2/index.php"),
            data=fields,
        )
        html = searched.text or ""
        Path("uploads/cache/plano_compras_after_search.html").write_text(
            html, encoding="utf-8", errors="ignore"
        )
        fields2 = fields_from(html)
        send_filter = fields2.get("sendFilter64") or fields.get("sendFilter64") or ""
        print("sendFilter64 len", len(send_filter))
        print("no results?", "No se encontraron resultados" in html)

        cmds = [
            f"cmd=getlibro&hash={send_filter}",
            f"&cmd=getlibro&hash={send_filter}",
            f"cmd=getSummaryDataVenta&filtro={send_filter}&modulo=COMPRA",
            "cmd=getSummaryDataVenta&modulo=COMPRA",
            "cmd=version",
        ]
        for cmd in cmds:
            response = db_send(client, "form/contabilidad/libros2/command.php", cmd)
            text = response.text or ""
            Path(
                "uploads/cache/plano_db_"
                + re.sub(r"[^a-z0-9]+", "_", cmd[:40])
                + ".xml"
            ).write_text(text, encoding="utf-8", errors="ignore")
            print(
                "CMD",
                cmd[:60],
                response.status_code,
                "len",
                len(text),
                "start",
                " ".join(text.split())[:250],
            )


if __name__ == "__main__":
    main()
