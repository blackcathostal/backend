"""Scrape facturacion.cl web Libro de Compras (recibidas).

Uses the real company portal (e.g. /blackcat), not Plano test (/plano/).
SOAP ObtenerCompra requires an uncontracted add-on.
"""
from __future__ import annotations

import re
from datetime import date
from html import unescape
from typing import Any
from urllib.parse import unquote, urljoin

import httpx

from app.core.config import settings
from app.services.facturacion_client import FacturacionClient, FacturacionError, _parse_amount, _parse_date


class FacturacionWebClient:
    def __init__(
        self,
        *,
        empresa: str | None = None,
        usuario: str | None = None,
        clave: str | None = None,
        timeout: float = 90.0,
    ) -> None:
        self.empresa = (
            empresa
            if empresa is not None
            else (
                settings.facturacion_web_empresa
                or settings.facturacion_prod_rut
                or ""
            )
        ).strip()
        self.usuario = (
            usuario
            if usuario is not None
            else (settings.facturacion_web_user or "")
        ).strip()
        self.clave = (
            clave if clave is not None else (settings.facturacion_web_clave or "")
        ).strip()
        self.timeout = timeout
        self._client: httpx.Client | None = None
        self.base_url = "https://www.facturacion.cl/"

    def require_credentials(self) -> None:
        missing = [
            name
            for name, value in (
                ("FACTURACION_WEB_EMPRESA", self.empresa),
                ("FACTURACION_WEB_USER", self.usuario),
                ("FACTURACION_WEB_CLAVE", self.clave),
            )
            if not value
        ]
        if missing:
            raise FacturacionError(
                "Faltan credenciales web de facturacion.cl: " + ", ".join(missing)
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

    def _acceso_send(self, parameter: str) -> str:
        client = self._ensure_client()
        response = client.post(
            "https://www.facturacion.cl/desis/accesoFE3.php",
            content=(parameter + "&commandfile=").encode("latin-1", errors="ignore"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=ISO-8859-1",
                "AXJAGENT": "LOGIN",
            },
        )
        return response.text or ""

    def login(self) -> None:
        self.require_credentials()
        client = self._ensure_client()
        client.get("https://www.facturacion.cl/")

        empresa = self.empresa.upper()
        usuario = self.usuario.upper()
        text = self._acceso_send(
            f"&cmd=post&empresa=+{empresa}&usuario={usuario}&password={self.clave}"
        )
        fault = re.search(r'error="([^"]*)"', text, re.I)
        if fault and fault.group(1).strip():
            raise FacturacionError(
                f"Login web facturacion.cl falló: {fault.group(1).strip()}"
            )
        web_match = re.search(r"<web[^>]*>([^<]*)</web>", text, re.I)
        if not web_match or not web_match.group(1).strip():
            raise FacturacionError(
                "Login web facturacion.cl no devolvió el host del sistema. "
                "Revisá FACTURACION_WEB_EMPRESA / USER / CLAVE."
            )
        host = unquote(web_match.group(1).strip()).rstrip("/")
        self.base_url = f"https://{host}/"
        response = client.post(
            urljoin(self.base_url, "index.php"),
            data={
                "empresa": empresa,
                "user": usuario,
                "pass": self.clave,
            },
        )
        text = response.text or ""
        if "Libro" not in text and "contabilidad" not in text.lower():
            raise FacturacionError(
                "Login web facturacion.cl no abrió el sistema de la empresa."
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
            raise FacturacionError(
                "No se pudo abrir Libro de Compras en facturacion.cl"
            )
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

    @staticmethod
    def _parse_spanish_date(value: str) -> str | None:
        text = " ".join(str(value or "").split())
        if not text:
            return None
        months = {
            "enero": 1,
            "febrero": 2,
            "marzo": 3,
            "abril": 4,
            "mayo": 5,
            "junio": 6,
            "julio": 7,
            "agosto": 8,
            "septiembre": 9,
            "setiembre": 9,
            "octubre": 10,
            "noviembre": 11,
            "diciembre": 12,
        }
        match = re.search(
            r"(\d{1,2})\s+de\s+([A-Za-zÁÉÍÓÚáéíóú]+)\s+de\s+(\d{4})",
            text,
            re.I,
        )
        if not match:
            return None
        day = int(match.group(1))
        month = months.get(match.group(2).lower())
        year = int(match.group(3))
        if not month:
            return None
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    def _parse_grid_html(self, html: str) -> list[dict[str, Any]]:
        if "No se encontraron resultados" in html:
            return []

        rows: list[dict[str, Any]] = []
        # Real portal uses tr_lista_fila11 / tr_lista_fila22 (not tr_lista_fila).
        for row_html in re.findall(
            r"<tr[^>]*class=['\"]tr_lista_fila\d*['\"][^>]*>([\s\S]*?)</tr>",
            html,
            re.I,
        ):
            cells: dict[str, str] = {}
            for td in re.findall(r"<td([^>]*)>([\s\S]*?)</td>", row_html, re.I):
                attrs, body = td
                name_match = re.search(r'name=["\']([^"\']+)["\']', attrs, re.I)
                if not name_match:
                    continue
                name = name_match.group(1).upper()
                if name == "FOLIO":
                    value_match = re.search(
                        r'<input[^>]+value=["\']([^"\']+)["\']',
                        body,
                        re.I,
                    )
                    cells[name] = (
                        value_match.group(1).strip()
                        if value_match
                        else self._strip_html(body)
                    )
                    pdf_match = re.search(
                        r"vistaprevia\.php\?data=([^\"'\s]+)",
                        body,
                        re.I,
                    )
                    if pdf_match:
                        cells["PDF_DATA"] = pdf_match.group(1).strip()
                elif name == "DOCUMENTO":
                    cells[name] = self._strip_html(body)
                    if "PDF_DATA" not in cells:
                        pdf_match = re.search(
                            r"vistaprevia\.php\?data=([^\"'\s]+)",
                            body,
                            re.I,
                        )
                        if pdf_match:
                            cells["PDF_DATA"] = pdf_match.group(1).strip()
                elif name == "RAZONSOCIAL":
                    rut_match = re.search(
                        r"seenopago\(['\"]([^'\"]+)['\"]\)",
                        body,
                        re.I,
                    )
                    if rut_match:
                        cells["RUT"] = rut_match.group(1).strip()
                    cells[name] = self._strip_html(body)
                else:
                    cells[name] = self._strip_html(body)

            folio = cells.get("FOLIO") or ""
            rut = cells.get("RUT") or ""
            if not folio and not rut:
                continue
            fecha = self._parse_spanish_date(cells.get("FECHA") or "") or cells.get("FECHA")
            rows.append(
                {
                    "folio": folio,
                    "tipo": cells.get("DOCUMENTO") or "",
                    "fecha": fecha,
                    "razonsocial": cells.get("RAZONSOCIAL") or "",
                    "montototal": cells.get("TOTAL") or "",
                    "rut": rut,
                    "pdf_data": cells.get("PDF_DATA") or "",
                }
            )
        return rows

    def download_pdf(self, pdf_data: str) -> bytes:
        """Fetch PDF bytes for a Libro de Compras document token."""
        token = (pdf_data or "").strip()
        if not token:
            raise FacturacionError("Documento sin token PDF")
        self.login()
        client = self._ensure_client()
        response = client.get(urljoin(self.base_url, f"vistaprevia.php?data={token}"))
        body = response.content or b""
        content_type = (response.headers.get("content-type") or "").lower()
        if response.status_code >= 400 or not body.startswith(b"%PDF"):
            raise FacturacionError(
                f"No se pudo descargar el PDF (HTTP {response.status_code}, {content_type})"
            )
        return body

    def fetch_recibidas(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        """Pull Libro de Compras month-by-month from company web UI."""
        if date_to < date_from:
            raise FacturacionError("Rango de fechas inválido")

        self.login()
        _, base_fields = self._load_compras_form()
        normalizer = FacturacionClient()
        docs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()

        cursor = date(date_from.year, date_from.month, 1)
        while cursor <= date_to:
            html = self._search_month(
                year=cursor.year, month=cursor.month, base_fields=base_fields
            )
            month_fields = self._extract_fields(html) or base_fields
            batch_raw = self._parse_grid_html(html)

            month_docs: list[dict[str, Any]] = []
            for raw in batch_raw:
                payload = {
                    "folio": raw.get("folio"),
                    "tipo": raw.get("tipo") or raw.get("doc_type"),
                    "fecha": raw.get("fecha"),
                    "razonsocial": raw.get("razonsocial"),
                    "montototal": raw.get("montototal"),
                    "montoneto": raw.get("montoneto") or raw.get("neto"),
                    "montoiva": raw.get("montoiva") or raw.get("iva"),
                    "montoexento": raw.get("montoexento") or raw.get("exento"),
                    "rut": raw.get("rut") or raw.get("rutproveedor"),
                    "pdf_data": raw.get("pdf_data") or "",
                }
                doc = normalizer._normalize_doc(payload, direction="recibida")
                if not doc["total_amount"] and raw.get("montototal") is not None:
                    doc["total_amount"] = _parse_amount(raw.get("montototal"))
                month_docs.append(doc)

            if not month_docs:
                excel = self._download_excel(
                    year=cursor.year, month=cursor.month, fields=month_fields
                )
                if excel:
                    filename = (
                        "libro_compras.xlsx" if excel[:2] == b"PK" else "libro_compras.xls"
                    )
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
