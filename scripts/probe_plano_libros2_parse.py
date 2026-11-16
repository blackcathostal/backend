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


def main() -> None:
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        login(client)
        # indice=2 => Libro Compras per menu
        response = client.get(urljoin(BASE, "form/contabilidad/libros2/?indice=2&fromMenu=true"))
        html = response.text or ""
        Path("uploads/cache/plano_libro_compras_indice2.html").write_text(
            html, encoding="utf-8", errors="ignore"
        )
        print("status", response.status_code, "len", len(html))

        for pat, label in (
            (r"<form[^>]*>.*?</form>", "FORM"),
            (r"<input[^>]+>", "INPUT"),
            (r"<select[^>]*>.*?</select>", "SELECT"),
            (r"<a[^>]*>.*?</a>", "A"),
            (r"<button[^>]*>.*?</button>", "BUTTON"),
        ):
            matches = re.findall(pat, html, re.I | re.S)
            print(label, "count", len(matches))
            for item in matches[:40]:
                text = " ".join(item.split())
                if any(
                    k in text.lower()
                    for k in (
                        "excel",
                        "export",
                        "xlsx",
                        "csv",
                        "mes",
                        "anio",
                        "año",
                        "periodo",
                        "indice",
                        "libro",
                        "compra",
                        "buscar",
                        "generar",
                        "descarg",
                        "submit",
                        "name=",
                    )
                ):
                    print(" ", text[:350])

        # JS calls that look like export
        for match in re.finditer(
            r".{0,60}(excel|exportar|xlsx|csv|descarg|libro).{0,80}",
            html,
            re.I | re.S,
        ):
            print("HIT", " ".join(match.group(0).split())[:300])

        # Also try indice=1 ventas for comparison size/structure
        ventas = client.get(urljoin(BASE, "form/contabilidad/libros2/?indice=1&fromMenu=true"))
        Path("uploads/cache/plano_libro_ventas_indice1.html").write_text(
            ventas.text or "", encoding="utf-8", errors="ignore"
        )
        print("ventas len", len(ventas.text or ""))


if __name__ == "__main__":
    main()
