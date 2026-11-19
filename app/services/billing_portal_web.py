"""Scrape billing portal purchase ledger (received tax documents).

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
from app.services.billing_portal_client import BillingPortalClient, BillingPortalError, _parse_amount, _parse_date


class BillingPortalWebClient:
    def __init__(
        self,
        *,
        company: str | None = None,
        user: str | None = None,
        password: str | None = None,
        timeout: float = 90.0,
    ) -> None:
        self.company = (
            company
            if company is not None
            else (
                settings.billing_portal_web_company
                or settings.billing_portal_prod_rut
                or ""
            )
        ).strip()
        self.user = (
            user
            if user is not None
            else (settings.billing_portal_web_user or "")
        ).strip()
        self.password = (
            password if password is not None else (settings.billing_portal_web_password or "")
        ).strip()
        self.timeout = timeout
        self._client: httpx.Client | None = None
        self.base_url = "https://www.facturacion.cl/"

    def require_credentials(self) -> None:
        missing = [
            name
            for name, value in (
                ("BILLING_PORTAL_WEB_COMPANY", self.company),
                ("BILLING_PORTAL_WEB_USER", self.user),
                ("BILLING_PORTAL_WEB_PASSWORD", self.password),
            )
            if not value
        ]
        if missing:
            raise BillingPortalError(
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

    def __enter__(self) -> "BillingPortalWebClient":
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

        empresa = self.company.upper()
        usuario = self.user.upper()
        text = self._acceso_send(
            f"&cmd=post&empresa=+{empresa}&usuario={usuario}&password={self.password}"
        )
        fault = re.search(r'error="([^"]*)"', text, re.I)
        if fault and fault.group(1).strip():
            raise BillingPortalError(
                f"Login web facturacion.cl falló: {fault.group(1).strip()}"
            )
        web_match = re.search(r"<web[^>]*>([^<]*)</web>", text, re.I)
        if not web_match or not web_match.group(1).strip():
            raise BillingPortalError(
                "Login web facturacion.cl no devolvió el host del sistema. "
                "Check BILLING_PORTAL_WEB_COMPANY / USER / PASSWORD "
                "(or legacy FACTURACION_WEB_*)."
            )
        host = unquote(web_match.group(1).strip()).rstrip("/")
        self.base_url = f"https://{host}/"
        response = client.post(
            urljoin(self.base_url, "index.php"),
            data={
                "empresa": empresa,
                "user": usuario,
                "pass": self.password,
            },
        )
        text = response.text or ""
        if "Libro" not in text and "contabilidad" not in text.lower():
            raise BillingPortalError(
                "Login web facturacion.cl no abrió el sistema de la empresa."
            )

    @staticmethod
    def _extract_chilean_rut(value: object) -> str:
        """Pull a Chilean RUT from HTML/text (ventas grid often embeds it outside seenopago)."""
        text = unescape(str(value or ""))
        # Prefer explicit attributes / JS hooks used by the portal.
        for pattern in (
            r'data-rut=["\']([^"\']+)["\']',
            r"(?:seenopago|vercliente|verreceptor|verproveedor|detallecliente)\(\s*['\"]([^'\"]+)['\"]",
            r"rut[=:]\s*['\"]?(\d{1,2}\.?\d{3}\.?\d{3}-[\dkK])",
        ):
            match = re.search(pattern, text, flags=re.I)
            if match:
                return re.sub(r"\s+", "", match.group(1)).strip()
        match = re.search(
            r"\b(\d{1,2}(?:\.\d{3}){2}-[\dkK])\b",
            text,
            flags=re.I,
        )
        if match:
            return match.group(1).strip()
        match = re.search(r"\b(\d{7,8}-[\dkK])\b", text, flags=re.I)
        if match:
            return match.group(1).strip()
        return ""

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

    def _load_ledger_form(self, *, indice: str = "2") -> tuple[str, dict[str, str]]:
        """Open libros2 ledger. indice 1=Ventas, 2=Compras."""
        book = str(indice or "2").strip() or "2"
        client = self._ensure_client()
        response = client.get(
            urljoin(
                self.base_url,
                f"form/contabilidad/libros2/?indice={book}&fromMenu=true",
            )
        )
        html = response.text or ""
        label = "Libro de Ventas" if book == "1" else "Libro de Compras"
        if label not in html and "indice" not in html.lower() and "Libro" not in html:
            raise BillingPortalError(
                f"No se pudo abrir {label} en facturacion.cl"
            )
        return html, self._extract_fields(html)

    def _load_compras_form(self) -> tuple[str, dict[str, str]]:
        return self._load_ledger_form(indice="2")

    def _load_ventas_form(self) -> tuple[str, dict[str, str]]:
        return self._load_ledger_form(indice="1")

    def _search_month(
        self,
        *,
        year: int,
        month: int,
        base_fields: dict[str, str],
        page: int = 1,
        folio: str = "",
        rut: str = "",
        page_size: int = 100,
        indice: str = "2",
    ) -> str:
        client = self._ensure_client()
        size = str(max(30, min(int(page_size or 100), 500)))
        book = str(indice or base_fields.get("indice") or "2").strip() or "2"
        payload = {
            **base_fields,
            "indice": book,
            "anno": str(year),
            "mes": f"{month:02d}",
            "tipoinformacion": base_fields.get("tipoinformacion") or "1",
            "GRID_NumReg": size,
            "GRID_NumRegAux": size,
            "GRID_segundaCarga": "true",
            "GRID_Page": str(max(1, int(page or 1))),
            "GRID_Action": "",
            "GRID_OrderBy": "",
            "GRID_OrderType": "",
            "rut": (rut or "").strip(),
            "folio": (folio or "").strip(),
            "razonsocial": "",
        }
        response = client.post(
            urljoin(self.base_url, "form/contabilidad/libros2/index.php"),
            data=payload,
        )
        return response.text or ""

    def _iter_month_rows(
        self,
        *,
        year: int,
        month: int,
        base_fields: dict[str, str],
        folio: str = "",
        rut: str = "",
        indice: str = "2",
    ) -> list[dict[str, Any]]:
        """Fetch all grid pages for a month (portal often caps ~30/page)."""
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        page_size = 100
        book = str(indice or "2").strip() or "2"
        for page in range(1, 21):
            html = self._search_month(
                year=year,
                month=month,
                base_fields=base_fields,
                page=page,
                folio=folio,
                rut=rut,
                page_size=page_size,
                indice=book,
            )
            batch = self._parse_grid_html(html)
            if not batch:
                break
            new_count = 0
            for item in batch:
                key = (str(item.get("folio") or ""), str(item.get("rut") or ""))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(item)
                new_count += 1
            if new_count == 0 or len(batch) < 30:
                break
        return rows

    def _download_excel(
        self,
        *,
        year: int,
        month: int,
        fields: dict[str, str],
        indice: str = "2",
    ) -> bytes | None:
        client = self._ensure_client()
        book = str(indice or fields.get("indice") or "2").strip() or "2"
        payload = {
            **fields,
            "indice": book,
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
    def _parse_local_date(value: str) -> str | None:
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
                elif name in {"RAZONSOCIAL", "CLIENTE", "RECEPTOR", "NOMBRE"}:
                    if not cells.get("RUT"):
                        found = self._extract_chilean_rut(body)
                        if found:
                            cells["RUT"] = found
                    cells["RAZONSOCIAL"] = self._strip_html(body)
                elif name in {"RUT", "RUTCLIENTE", "RUTRECEPTOR", "RUT_CLIENTE"}:
                    found = self._extract_chilean_rut(body) or self._strip_html(body)
                    if found:
                        cells["RUT"] = re.sub(r"\s+", "", found).strip()
                else:
                    cells[name] = self._strip_html(body)

            folio = cells.get("FOLIO") or ""
            rut = (
                cells.get("RUT")
                or self._extract_chilean_rut(cells.get("RAZONSOCIAL") or "")
                or self._extract_chilean_rut(row_html)
            )
            if not folio and not rut:
                continue
            fecha = self._parse_local_date(cells.get("FECHA") or "") or cells.get("FECHA")
            company = cells.get("RAZONSOCIAL") or cells.get("CLIENTE") or ""
            # Drop trailing branch tags like [CASA MATRIZ] from display name only when RUT exists.
            rows.append(
                {
                    "folio": folio,
                    "tipo": cells.get("DOCUMENTO") or "",
                    "fecha": fecha,
                    "razonsocial": company,
                    "montototal": cells.get("TOTAL") or "",
                    "montoneto": cells.get("NETO") or cells.get("MONTONETO") or "",
                    "montoiva": cells.get("IVA") or cells.get("MONTOIVA") or "",
                    "rut": rut,
                    "pdf_data": cells.get("PDF_DATA") or "",
                }
            )
        return rows

    def download_pdf(self, pdf_data: str) -> bytes:
        """Fetch PDF bytes for a Libro de Compras document token."""
        token = (pdf_data or "").strip()
        if not token:
            raise BillingPortalError("Documento sin token PDF")
        self.login()
        client = self._ensure_client()
        response = client.get(urljoin(self.base_url, f"vistaprevia.php?data={token}"))
        body = response.content or b""
        content_type = (response.headers.get("content-type") or "").lower()
        if response.status_code >= 400 or not body.startswith(b"%PDF"):
            raise BillingPortalError(
                f"No se pudo descargar el PDF (HTTP {response.status_code}, {content_type})"
            )
        return body

    @staticmethod
    def _norm_key(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()

    def find_pdf_data(
        self,
        *,
        folio: str,
        rut: str = "",
        issued_at: date | None = None,
    ) -> str:
        """Locate preview token for a received document by folio (and optional RUT/month)."""
        folio_key = self._norm_key(folio)
        if not folio_key:
            return ""
        rut_key = self._norm_key(rut)
        self.login()
        _, base_fields = self._load_compras_form()

        months: list[tuple[int, int]] = []
        if issued_at:
            months.append((issued_at.year, issued_at.month))
        else:
            today = date.today()
            cursor = date(today.year, today.month, 1)
            for _ in range(4):
                months.append((cursor.year, cursor.month))
                if cursor.month == 1:
                    cursor = date(cursor.year - 1, 12, 1)
                else:
                    cursor = date(cursor.year, cursor.month - 1, 1)

        for year, month in months:
            rows = self._iter_month_rows(
                year=year,
                month=month,
                base_fields=base_fields,
                folio=str(folio or "").strip(),
                rut=str(rut or "").strip(),
            )
            if not rows and folio_key:
                # Folio filter sometimes fails; fall back to full month pages.
                rows = self._iter_month_rows(
                    year=year, month=month, base_fields=base_fields
                )
            for row in rows:
                if self._norm_key(str(row.get("folio") or "")) != folio_key:
                    continue
                if rut_key and self._norm_key(str(row.get("rut") or "")) not in {
                    "",
                    rut_key,
                }:
                    continue
                token = str(row.get("pdf_data") or "").strip()
                if token:
                    return token
        return ""

    def _fetch_ledger_range(
        self,
        date_from: date,
        date_to: date,
        *,
        direction: str,
        indice: str,
        excel_name: str,
    ) -> list[dict[str, Any]]:
        if date_to < date_from:
            raise BillingPortalError("Rango de fechas inválido")

        self.login()
        book = str(indice or "2").strip() or "2"
        _, base_fields = self._load_ledger_form(indice=book)
        normalizer = BillingPortalClient()
        docs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()

        cursor = date(date_from.year, date_from.month, 1)
        while cursor <= date_to:
            batch_raw = self._iter_month_rows(
                year=cursor.year,
                month=cursor.month,
                base_fields=base_fields,
                indice=book,
            )
            month_fields = base_fields

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
                doc = normalizer._normalize_doc(payload, direction=direction)
                if not doc["total_amount"] and raw.get("montototal") is not None:
                    doc["total_amount"] = _parse_amount(raw.get("montototal"))
                month_docs.append(doc)

            if not month_docs:
                html = self._search_month(
                    year=cursor.year,
                    month=cursor.month,
                    base_fields=base_fields,
                    indice=book,
                )
                month_fields = self._extract_fields(html) or base_fields
                excel = self._download_excel(
                    year=cursor.year,
                    month=cursor.month,
                    fields=month_fields,
                    indice=book,
                )
                if excel:
                    filename = (
                        f"{excel_name}.xlsx" if excel[:2] == b"PK" else f"{excel_name}.xls"
                    )
                    try:
                        month_docs = normalizer.parse_ledger_file(
                            excel, direction=direction, filename=filename
                        )
                    except BillingPortalError:
                        month_docs = []

            for doc in month_docs:
                issued = _parse_date(doc.get("issued_at"))
                if issued and (issued < date_from or issued > date_to):
                    continue
                key = (
                    str(doc.get("dte_type_code") or doc.get("dte_type_id") or ""),
                    str(doc.get("folio") or ""),
                    str(doc.get("identification_number") or doc.get("counterpart_rut") or ""),
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

    def fetch_received(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        """Pull Libro de Compras month-by-month from company web UI."""
        return self._fetch_ledger_range(
            date_from,
            date_to,
            direction="received",
            indice="2",
            excel_name="libro_compras",
        )

    def fetch_issued(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        """Pull Libro de Ventas (issued DTEs) month-by-month from company web UI."""
        return self._fetch_ledger_range(
            date_from,
            date_to,
            direction="issued",
            indice="1",
            excel_name="libro_ventas",
        )
