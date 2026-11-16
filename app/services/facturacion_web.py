"""Scrape facturacion.cl Plano web UI for Libro de Compras (recibidas).

SOAP ObtenerCompra / Panel DTE require add-on modules not contracted.
The Plano Contabilidad → Libro Compras screen remains available with web login.
"""
from __future__ import annotations

import re
from datetime import date
from html import unescape
from typing import Any
from urllib.parse import urljoin

import httpx

from app.core.config import settings
from app.services.facturacion_client import FacturacionClient, FacturacionError, _parse_amount, _parse_date


class FacturacionWebClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        usuario: str | None = None,
        clave: str | None = None,
        timeout: float = 90.0,
    ) -> None:
        self.base_url = (
            base_url or settings.facturacion_web_test_url or "https://www.facturacion.cl/plano/"
        ).rstrip("/") + "/"
        self.usuario = (
            usuario
            if usuario is not None
            else (settings.facturacion_web_test_user or settings.facturacion_user or "")
        ).strip()
        self.clave = (
            clave if clave is not None else (settings.facturacion_web_test_clave or "")
        ).strip()
        self.timeout = timeout
        self._client: httpx.Client | None = None

    def require_credentials(self) -> None:
        missing = [
            name
            for name, value in (
                ("FACTURACION_WEB_TEST_URL", self.base_url),
                ("FACTURACION_WEB_TEST_USER", self.usuario),
                ("FACTURACION_WEB_TEST_CLAVE", self.clave),
            )
            if not value
        ]
        if missing:
            raise FacturacionError(
                "Faltan credenciales web de facturacion.cl (Plano): " + ", ".join(missing)
            )

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; BlackCatAdmin/1.0; +https://blackcathostal.com)"
                    )
                },
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "FacturacionWebClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def login(self) -> None:
        self.require_credentials()
        client = self._ensure_client()
        page = client.get(self.base_url)
        action_match = re.search(
            r'<form[^>]+action=(["\'])([^"\']*)\1',
            page.text or "",
            re.I,
        )
        action = urljoin(str(page.url), action_match.group(2) if action_match else "index.php")
        response = client.post(
            action,
            data={
                "user": self.usuario,
                "pass": self.clave,
                "Submit": "Ingresar",
                "href": "",
                "urlRedirect": "",
            },
        )
        text = response.text or ""
        if "Libro Compras" not in text and "libro" not in text.lower():
            raise FacturacionError(
                "Login web facturacion.cl (Plano) falló. "
                "Revisá FACTURACION_WEB_TEST_USER / FACTURACION_WEB_TEST_CLAVE."
            )

    @staticmethod
    def _strip_html(value: str) -> str:
        text = re.sub(r"<[^>]+>", " ", value or "")
        text = unescape(text)
        return " ".join(text.split())

    @staticmethod
    def _extract_fields(html: str) -> dict[str, str]:
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

    def _load_compras_form(self) -> tuple[str, dict[str, str]]:
        client = self._ensure_client()
        response = client.get(
            urljoin(self.base_url, "form/contabilidad/libros2/?indice=2&fromMenu=true")
        )
        html = response.text or ""
        if "Libro de Compras" not in html and "indice" not in html.lower():
            raise FacturacionError("No se pudo abrir Libro de Compras en facturacion.cl Plano")
        return html, self._extract_fields(html)

    def _search_month(self, *, year: int, month: int, base_fields: dict[str, str]) -> str:
        client = self._ensure_client()
        payload = {
            **base_fields,
            "indice": "2",
            "anno": str(year),
            "mes": f"{month:02d}",
            "tipoinformacion": base_fields.get("tipoinformacion") or "1",
            "GRID_NumReg": "500",
            "GRID_NumRegAux": "500",
            "GRID_segundaCarga": "true",
            "GRID_Page": "1",
            "GRID_Action": "",
            "GRID_OrderBy": "",
            "GRID_OrderType": "",
            "rut": "",
            "folio": "",
            "razonsocial": "",
        }
        response = client.post(
            urljoin(self.base_url, "form/contabilidad/libros2/index.php"),
            data=payload,
        )
        return response.text or ""

    def _download_excel(self, *, year: int, month: int, fields: dict[str, str]) -> bytes | None:
        client = self._ensure_client()
        payload = {
            **fields,
            "indice": "2",
            "anno": str(year),
            "mes": f"{month:02d}",
            "tipoinformacion": fields.get("tipoinformacion") or "1",
            "GRID_NumReg": "500",
            "GRID_NumRegAux": "500",
            "GRID_segundaCarga": "true",
            "GRID_Page": "1",
            "GRID_Action": "excel",
        }
        response = client.post(
            urljoin(self.base_url, "form/contabilidad/libros2/index.php"),
            data=payload,
        )
        content_type = (response.headers.get("content-type") or "").lower()
        disposition = (response.headers.get("content-disposition") or "").lower()
        body = response.content or b""
        if not body:
            return None
        if "html" in content_type and b"<html" in body[:200].lower():
            return None
        if (
            "excel" in content_type
            or "spreadsheet" in content_type
            or "octet-stream" in content_type
            or ".xls" in disposition
            or body[:2] == b"PK"
            or body[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        ):
            return body
        return None

    def _parse_grid_html(self, html: str) -> list[dict[str, Any]]:
        if "No se encontraron resultados" in html:
            return []

        grid_match = re.search(
            r'class=(["\'])LIBROS_GRID\1[\s\S]*?<table[^>]*>([\s\S]*?)</table>',
            html,
            re.I,
        )
        if not grid_match:
            return []
        table = grid_match.group(2)
        header_row = re.search(r"<tr[^>]*id=['\"]GRID_Header['\"][^>]*>([\s\S]*?)</tr>", table, re.I)
        if not header_row:
            return []
        headers = [
            self._strip_html(th).lower()
            for th in re.findall(r"<th[^>]*>([\s\S]*?)</th>", header_row.group(1), re.I)
        ]
        if not headers:
            return []

        def col(*names: str) -> int | None:
            for name in names:
                for idx, header in enumerate(headers):
                    if name in header:
                        return idx
            return None

        idx_folio = col("folio")
        idx_doc = col("documento")
        idx_fecha = col("fecha documento", "fecha")
        idx_razon = col("razón social", "razon social", "raz&oacute;n social")
        idx_total = col("monto total", "total")
        idx_rut = col("rut")

        rows: list[dict[str, Any]] = []
        for row_html in re.findall(r"<tr[^>]*class=['\"]tr_lista_fila[^'\"]*['\"][^>]*>([\s\S]*?)</tr>", table, re.I):
            cells = [
                self._strip_html(td)
                for td in re.findall(r"<td[^>]*>([\s\S]*?)</td>", row_html, re.I)
            ]
            if not cells:
                continue
            if any("no se encontraron" in c.lower() for c in cells):
                continue

            def pick(index: int | None) -> str:
                if index is None or index >= len(cells):
                    return ""
                return cells[index]

            raw = {
                "folio": pick(idx_folio),
                "tipo": pick(idx_doc),
                "fecha": pick(idx_fecha),
                "razonsocial": pick(idx_razon),
                "montototal": pick(idx_total),
                "rut": pick(idx_rut),
            }
            if not raw["folio"] and not raw["rut"]:
                continue
            rows.append(raw)
        return rows

    def fetch_recibidas(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        """Pull Libro de Compras month-by-month from Plano web UI."""
        if date_to < date_from:
            raise FacturacionError("Rango de fechas inválido")

        self.login()
        _, base_fields = self._load_compras_form()
        normalizer = FacturacionClient()
        docs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()

        cursor = date(date_from.year, date_from.month, 1)
        while cursor <= date_to:
            html = self._search_month(year=cursor.year, month=cursor.month, base_fields=base_fields)
            month_fields = self._extract_fields(html) or base_fields
            batch_raw = self._parse_grid_html(html)

            month_docs: list[dict[str, Any]] = []
            for raw in batch_raw:
                doc = normalizer._normalize_doc(
                    {
                        "folio": raw.get("folio"),
                        "tipo": raw.get("tipo") or raw.get("doc_type"),
                        "fecha": raw.get("fecha"),
                        "razonsocial": raw.get("razonsocial"),
                        "montototal": raw.get("montototal"),
                        "montoneto": raw.get("montoneto") or raw.get("neto"),
                        "montoiva": raw.get("montoiva") or raw.get("iva"),
                        "montoexento": raw.get("montoexento") or raw.get("exento"),
                        "rut": raw.get("rut") or raw.get("rutproveedor"),
                    },
                    direction="recibida",
                )
                if not doc["total_amount"] and raw.get("montototal") is not None:
                    doc["total_amount"] = _parse_amount(raw.get("montototal"))
                month_docs.append(doc)

            if not month_docs:
                excel = self._download_excel(
                    year=cursor.year, month=cursor.month, fields=month_fields
                )
                if excel:
                    filename = "libro_compras.xlsx" if excel[:2] == b"PK" else "libro_compras.xls"
                    try:
                        month_docs = normalizer.parse_libro_file(
                            excel, direction="recibida", filename=filename
                        )
                    except FacturacionError:
                        month_docs = []

            for doc in month_docs:
                issued = _parse_date(doc.get("issued_at"))
                if issued and (issued < date_from or issued > date_to):
                    continue
                key = (
                    str(doc.get("doc_type") or ""),
                    str(doc.get("folio") or ""),
                    str(doc.get("counterpart_rut") or ""),
                )
                if key in seen:
                    continue
                if not key[1] and not key[2]:
                    continue
                seen.add(key)
                docs.append(doc)

            if cursor.month == 12:
                cursor = date(cursor.year + 1, 1, 1)
            else:
                cursor = date(cursor.year, cursor.month + 1, 1)

        return docs
