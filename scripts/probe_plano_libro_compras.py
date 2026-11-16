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
    if "Libro Compras" not in (response.text or "") and "libro" not in (response.text or "").lower():
        raise SystemExit(f"Login plano falló: {response.status_code} {response.url}")
    print("login ok", response.url, "cookie", list(client.cookies.keys()))


def dump(name: str, response: httpx.Response) -> Path:
    path = Path("uploads/cache") / name
    path.write_text(response.text or "", encoding="utf-8", errors="ignore")
    print(
        name,
        response.status_code,
        str(response.url),
        "len",
        len(response.text or ""),
        "ct",
        response.headers.get("content-type"),
    )
    return path


def main() -> None:
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        login(client)

        # Fetch menu JS / general helpers
        for rel in (
            "js/general.js?v=3.2",
            "componente/menu/controlador.js?v=25.11.04",
            "js/util.js?v=14.6",
            "form/contabilidad/libros2/",
            "form/contabilidad/libros2/index.php",
            "form/contabilidad/libros2/?tipo=2",
            "form/contabilidad/libros2/?libro=2",
        ):
            try:
                response = client.get(urljoin(BASE, rel))
                safe = rel.replace("/", "_").replace("?", "_").replace("=", "_")
                dump(f"plano_{safe}.html", response)
            except Exception as exc:  # noqa: BLE001
                print("GET fail", rel, exc)

        # Typical DESIS/plano AJAX patterns
        candidates = [
            ("POST", "form/contabilidad/libros2/", {"tipo": "2", "nivel": "3"}),
            ("POST", "form/contabilidad/libros2/index.php", {"tipo": "2"}),
            ("POST", "index.php", {"ruta": "form/contabilidad/libros2/", "p1": "2", "p2": "3"}),
            (
                "POST",
                "componente/mascara/class.php",
                {"ruta": "form/contabilidad/libros2/", "param1": "2", "param2": "3"},
            ),
            (
                "GET",
                "form/contabilidad/libros2/index.php",
                {"tipoLibro": "2", "mes": "11", "anio": "2025"},
            ),
        ]
        for method, path, data in candidates:
            url = urljoin(BASE, path)
            try:
                if method == "POST":
                    response = client.post(url, data=data)
                else:
                    response = client.get(url, params=data)
                safe = path.replace("/", "_") + "_" + method.lower()
                dump(f"plano_try_{safe}.html", response)
                text = response.text or ""
                keys = [
                    k
                    for k in (
                        "excel",
                        "export",
                        "xlsx",
                        "csv",
                        "periodo",
                        "mes",
                        "anio",
                        "compra",
                        "folio",
                        "proveedor",
                    )
                    if k in text.lower()
                ]
                print("  keywords", keys)
                inputs = re.findall(
                    r'<input[^>]+name=["\']([^"\']+)["\']',
                    text,
                    re.I,
                )
                selects = re.findall(
                    r'<select[^>]+name=["\']([^"\']+)["\']',
                    text,
                    re.I,
                )
                print("  inputs", inputs[:30], "selects", selects[:20])
            except Exception as exc:  # noqa: BLE001
                print("try fail", path, exc)

        # Search general.js for refresca_formulario_Menu definition
        js = client.get(urljoin(BASE, "js/general.js?v=3.2")).text or ""
        Path("uploads/cache/plano_general.js").write_text(js, encoding="utf-8", errors="ignore")
        for match in re.finditer(r"function\s+refresca_formulario_Menu\s*\([^)]*\)\s*\{", js):
            print("FOUND in general.js", js[match.start() : match.start() + 800])
        if "refresca_formulario_Menu" in js:
            idx = js.find("refresca_formulario_Menu")
            print("general snippet", js[idx : idx + 900])

        menu_js = client.get(urljoin(BASE, "componente/menu/controlador.js?v=25.11.04")).text or ""
        Path("uploads/cache/plano_menu_controlador.js").write_text(
            menu_js, encoding="utf-8", errors="ignore"
        )
        if "refresca_formulario_Menu" in menu_js:
            idx = menu_js.find("refresca_formulario_Menu")
            print("menu snippet", menu_js[idx : idx + 900])
        for pat in ("refresca_formulario", "Ajax", "Request", "form/contabilidad"):
            print("menu_js count", pat, menu_js.lower().count(pat.lower()))


if __name__ == "__main__":
    main()
