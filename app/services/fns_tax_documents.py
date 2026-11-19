"""Scrape FNS Contabilidad tax/payment documents for admin Track refresh."""
from __future__ import annotations

import json
import re
from calendar import monthrange
from datetime import date, datetime
from html import unescape
from typing import Any, Iterator

from app.core.config import settings
from app.services.fns_web_client import FnsWebClient, FnsWebError


def _norm(text: str) -> str:
    value = (text or "").strip().lower()
    for src, dst in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n")):
        value = value.replace(src, dst)
    return value


def _money(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    negative = text.startswith("(") and text.endswith(")")
    raw = re.sub(r"[^\d,\.]", "", text)
    if not raw or not re.search(r"\d", raw):
        return 0.0
    try:
        if "," in raw:
            amount = float(raw.replace(".", "").replace(",", "."))
        elif raw.count(".") > 1 or (raw.count(".") == 1 and len(raw.split(".")[-1]) == 3):
            amount = float(raw.replace(".", ""))
        else:
            amount = float(raw)
    except ValueError:
        return 0.0
    return -amount if negative else amount


def _parse_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    match = re.search(r"(\d{2})/(\d{2})/(\d{4})", text)
    if match:
        try:
            return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        except ValueError:
            return None
    return None


def _form_fields(html: str) -> dict[str, str]:
    match = re.search(
        r'<form[^>]*name=["\']form_datos["\'][\s\S]*?</form>',
        html,
        flags=re.I,
    )
    source = match.group(0) if match else html
    fields: dict[str, str] = {}
    for item in re.finditer(r"<input([^>]*)>", source, flags=re.I):
        attrs = item.group(1)
        name_m = re.search(r'name=["\']([^"\']+)["\']', attrs, flags=re.I)
        if not name_m:
            continue
        value_m = re.search(r'value=["\']([^"\']*)["\']', attrs, flags=re.I)
        fields[name_m.group(1)] = value_m.group(1) if value_m else ""
    return fields


def _property_id_from_settings() -> str:
    return str(
        settings.fns_hotel_pms_id or settings.fns_hotel_id or settings.fns_pms_id or ""
    ).strip()


def _property_id_from_fields(fields: dict[str, str]) -> str:
    for key in ("var1", "id_alojamiento", "alojamiento_id"):
        value = str(fields.get(key) or "").strip()
        if value:
            return value
    return ""


def _require_property_id(fields: dict[str, str]) -> str:
    property_id = _property_id_from_fields(fields) or _property_id_from_settings()
    if not property_id:
        raise FnsWebError(
            "Falta el ID de alojamiento FNS (configurá FNS_HOTEL_PMS_ID / FNS_HOTEL_ID "
            "o iniciá sesión para leer var1 del formulario Contabilidad)"
        )
    return property_id


def _date_payload(date_from: date, date_to: date) -> dict[str, str]:
    return {
        "f_inicio": date_from.strftime("%d/%m/%Y"),
        "f_fin": date_to.strftime("%d/%m/%Y"),
        "fecha_desde": date_from.strftime("%d/%m/%Y"),
        "fecha_hasta": date_to.strftime("%d/%m/%Y"),
        "dia_inicio": f"{date_from.day:02d}",
        "mes_inicio": f"{date_from.month:02d}",
        "ano_inicio": str(date_from.year),
        "dia_fin": f"{date_to.day:02d}",
        "mes_fin": f"{date_to.month:02d}",
        "ano_fin": str(date_to.year),
    }


def _month_windows(date_from: date, date_to: date) -> Iterator[tuple[date, date]]:
    """Yield inclusive calendar-month windows covering [date_from, date_to]."""
    if date_to < date_from:
        return
    cursor = date(date_from.year, date_from.month, 1)
    while cursor <= date_to:
        last_day = monthrange(cursor.year, cursor.month)[1]
        window_start = max(date_from, cursor)
        window_end = min(date_to, date(cursor.year, cursor.month, last_day))
        yield window_start, window_end
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)


def _find_col(row: dict[str, str], *candidates: str) -> str:
    by_norm = {_norm(key): value for key, value in row.items()}
    for candidate in candidates:
        key = _norm(candidate)
        if key in by_norm:
            return str(by_norm[key] or "")
        for existing, value in by_norm.items():
            if key in existing or existing in key:
                return str(value or "")
    return ""


def _concept_breakdown(row: dict[str, str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in row.items():
        nk = _norm(key)
        if not any(
            token in nk
            for token in (
                "aloj",
                "lavan",
                "bar",
                "sauna",
                "frig",
                "caf",
                "deliv",
                "amen",
                "transfer",
                "park",
                "desay",
                "refri",
                "varios",
                "tasas",
            )
        ):
            continue
        amount = _money(value)
        if amount:
            out[key] = amount
    return out


def _parse_html_table(html: str, title_hint: str) -> list[dict[str, str]]:
    idx = html.lower().find(title_hint.lower())
    chunk = html[idx:] if idx >= 0 else html
    tables = re.findall(r"<table[^>]*>([\s\S]*?)</table>", chunk, flags=re.I)
    if not tables:
        return []
    table = max(tables, key=len)
    headers: list[str] = []
    rows: list[dict[str, str]] = []
    for row in re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", table, flags=re.I):
        cells = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cell)).strip()
            for cell in re.findall(r"<t[hd][^>]*>([\s\S]*?)</t[hd]>", row, flags=re.I)
        ]
        if not any(cells):
            continue
        if not headers and (
            "Alojamiento" in cells
            or "Importe" in cells
            or "Tipo documento" in cells
            or "Localizador" in cells
        ):
            headers = cells
            continue
        if not headers:
            continue
        item = {headers[i]: (cells[i] if i < len(cells) else "") for i in range(len(headers))}
        blob = " ".join(item.values()).lower()
        if "function" in blob:
            continue
        rows.append(item)
    return rows


class FnsTaxDocumentsClient:
    """Fetch issued/received Contabilidad documents via FNS web session."""

    def __init__(self, client: FnsWebClient | None = None) -> None:
        self._owns_client = client is None
        self.client = client or FnsWebClient()

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "FnsTaxDocumentsClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _post_report(self, *, module: str, action: str, date_from: date, date_to: date) -> str:
        self.client.ensure_session()
        fields = _form_fields(self.client.get_html("/es/Contabilidad"))
        property_id = _require_property_id(fields)
        http = self.client._ensure_client()
        payload = dict(fields)
        payload.update(_date_payload(date_from, date_to))
        payload.update(
            {
                "accion": action,
                "var1": property_id,
                "id_alojamiento": property_id,
                "alojamiento_id": property_id,
            }
        )
        response = http.post(f"{self.client.base_url}/es/{module}", data=payload)
        if response.status_code >= 400:
            raise FnsWebError(
                f"FNS Contabilidad HTTP {response.status_code} en {module}/{action}",
                details=response.text[:300],
            )
        if not self.client._is_authenticated_html(response.text):
            self.client.ensure_session(force_login=True)
            fields = _form_fields(self.client.get_html("/es/Contabilidad"))
            property_id = _require_property_id(fields)
            payload = dict(fields)
            payload.update(_date_payload(date_from, date_to))
            payload.update(
                {
                    "accion": action,
                    "var1": property_id,
                    "id_alojamiento": property_id,
                    "alojamiento_id": property_id,
                }
            )
            response = http.post(f"{self.client.base_url}/es/{module}", data=payload)
        return response.text

    def _normalize_cobro_row(self, row: dict[str, str], *, direction: str) -> dict[str, Any] | None:
        doc_type = _find_col(row, "Tipo documento", "Tipo")
        folio = _find_col(row, "Nº documento", "N° documento", "No documento", "Folio")
        folio = re.sub(r"\s+", "", folio)
        if not folio and not doc_type:
            # Payments without document still matter for account status updates by locator.
            folio = _find_col(row, "ID") or _find_col(row, "Localizador")
        if not folio:
            return None

        amount = _money(_find_col(row, "Importe", "Total", "Monto"))
        remaining = _find_col(row, "Restante", "Pendiente")
        remaining_amount = _money(remaining) if str(remaining or "").strip() else 0.0
        concepts = _concept_breakdown(row)
        has_concepts = bool(concepts)
        paid = remaining_amount <= 0.5 and amount > 0
        # If remaining is blank and there is importe, treat as collected.
        if not str(remaining or "").strip() and amount > 0:
            paid = True
        status_code = "paid" if paid else "pending_payment"
        # Keep FNS document type as-is; catalog resolves/creates against DB.
        raw_type = str(doc_type or "").strip()
        issued_at = _parse_date(_find_col(row, "Fecha", "Creado el"))
        company_name = _find_col(row, "Nombre", "Cliente", "Titular")
        locator = _find_col(row, "Localizador")
        return {
            "dte_type_code": raw_type,
            "doc_type": raw_type,
            "dte_version_code": "received" if direction == "received" else "issued",
            "folio": folio[:40],
            "identification_number": "",
            "company_name": company_name[:220],
            "counterpart_rut": "",
            "counterpart_name": company_name[:220],
            "issued_at": issued_at.isoformat() if issued_at else None,
            "net_amount": 0.0,
            "exempt_amount": 0.0,
            "tax_amount": 0.0,
            "total_amount": amount,
            "branch": "",
            "payment_status_code": status_code,
            "has_concepts": has_concepts,
            "concepts": concepts,
            "pms_ref": locator[:120],
            "pms_class": "FNS Cobros",
            "raw": {
                **row,
                "concepts": concepts,
                "payment_status_code": status_code,
                "remaining_amount": remaining_amount,
                "source_report": "Cobros",
                "doc_type": raw_type,
            },
        }

    def _client_name_by_locator(
        self, date_from: date, date_to: date
    ) -> dict[str, str]:
        """Map reservation locator -> customer name from Ingresos-Cobros."""
        html = self._post_report(
            module="Informes",
            action="ingresoscobros",
            date_from=date_from,
            date_to=date_to,
        )
        match = re.search(
            r'name=["\']json_tablas["\']\s+value=["\']([^"\']+)["\']',
            html,
            flags=re.I,
        )
        if not match:
            return {}
        try:
            payload = json.loads(unescape(match.group(1)).replace("&quot;", '"'))
            block = payload[0] if isinstance(payload, list) else payload
            headers = (block.get("cabecera") or {}).get("1") or []
            body = block.get("cuerpo") or {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        out: dict[str, str] = {}
        for item in body.values():
            if not isinstance(item, list):
                continue
            row = {
                headers[i]: item[i] if i < len(item) else None for i in range(len(headers))
            }
            locator = re.sub(r"\s+", "", str(row.get("Localizador") or ""))
            name = re.sub(r"\s+", " ", str(row.get("Datos cliente") or "")).strip()
            if locator and name:
                out[locator] = name
        return out

    def _merge_issued_row(
        self,
        merged: dict[tuple[str, str], dict[str, Any]],
        doc: dict[str, Any],
        *,
        client_by_locator: dict[str, str],
        date_from: date,
        date_to: date,
    ) -> None:
        issued = _parse_date(doc.get("issued_at"))
        if issued and (issued < date_from or issued > date_to):
            return
        locator = str(doc.get("pms_ref") or "").strip()
        ingreso_name = client_by_locator.get(locator, "")
        cobro_name = str(doc.get("company_name") or "").strip()
        # Prefer real guest name from Ingresos-Cobros when Cobros has empty/generic name.
        if ingreso_name and (
            not cobro_name or _norm(cobro_name) in {"consumos consumos", "consumos"}
        ):
            doc["company_name"] = ingreso_name
            doc["counterpart_name"] = ingreso_name
        key = (
            str(doc.get("doc_type") or doc.get("dte_type_code") or ""),
            str(doc.get("folio") or ""),
        )
        current = merged.get(key)
        if current is None:
            merged[key] = doc
            return
        # Same folio may appear in several payments: sum totals, keep paid if all settled.
        current["total_amount"] = float(current.get("total_amount") or 0) + float(
            doc.get("total_amount") or 0
        )
        concepts = dict(current.get("concepts") or {})
        for name, amount in (doc.get("concepts") or {}).items():
            concepts[name] = float(concepts.get(name) or 0) + float(amount or 0)
        current["concepts"] = concepts
        current["has_concepts"] = bool(concepts)
        if current.get("payment_status_code") != "paid" or doc.get("payment_status_code") != "paid":
            if doc.get("payment_status_code") == "pending_payment":
                current["payment_status_code"] = "pending_payment"
        raw = dict(current.get("raw") or {})
        raw["concepts"] = concepts
        raw["payment_status_code"] = current["payment_status_code"]
        current["raw"] = raw
        if doc.get("company_name") and (
            not current.get("company_name")
            or _norm(str(current.get("company_name"))) in {"consumos consumos", "consumos"}
        ):
            current["company_name"] = doc["company_name"]
            current["counterpart_name"] = doc["company_name"]

    def fetch_issued(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        """Issued sales docs from Informes/Cobros, month-by-month (12-month lookback safe)."""
        if date_to < date_from:
            raise FnsWebError("Rango de fechas inválido")
        # FNS Cobros truncates / ignores huge ranges; scrape each calendar month.
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for window_from, window_to in _month_windows(date_from, date_to):
            html = self._post_report(
                module="Informes",
                action="Cobros",
                date_from=window_from,
                date_to=window_to,
            )
            client_by_locator = self._client_name_by_locator(window_from, window_to)
            rows = _parse_html_table(html, "Informe de cobros")
            for row in rows:
                doc = self._normalize_cobro_row(row, direction="issued")
                if not doc:
                    continue
                self._merge_issued_row(
                    merged,
                    doc,
                    client_by_locator=client_by_locator,
                    date_from=date_from,
                    date_to=date_to,
                )
        return list(merged.values())

    def fetch_received(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        """
        Received Contabilidad documents from FNS (not facturacion.cl).

        FNS Contabilidad does not expose a supplier Libro de Compras like facturacion.cl.
        We reuse Cobros rows that are operationally registered without an issued tax type only
        when they carry document metadata usable as received track lines; otherwise return [].
        """
        if date_to < date_from:
            raise FnsWebError("Rango de fechas inválido")

        # Prefer explicit purchase-like modules when they eventually return rows.
        for module, action, title in (
            ("ComprasBodegas", "ComprasBodegas.listado", "Compras"),
            ("Informes", "iva", "IVA"),
            ("Libros", "libro_recepcion", "recepci"),
        ):
            try:
                html = self._post_report(
                    module=module, action=action, date_from=date_from, date_to=date_to
                )
            except FnsWebError:
                continue
            rows = _parse_html_table(html, title)
            docs: list[dict[str, Any]] = []
            for row in rows:
                folio = _find_col(row, "Folio", "Nº documento", "N° documento", "Documento")
                folio = re.sub(r"\s+", "", folio)
                if not folio:
                    continue
                amount = _money(_find_col(row, "Total", "Importe", "Monto", "Montototal"))
                issued_at = _parse_date(_find_col(row, "Fecha", "Fecha documento"))
                if issued_at and (issued_at < date_from or issued_at > date_to):
                    continue
                company_name = _find_col(row, "Proveedor", "Razón social", "Nombre", "Empresa")
                rut = _find_col(row, "RUT", "Rut proveedor", "Rut")
                raw_type = str(
                    _find_col(row, "Tipo", "Tipo documento", "Tipo DTE") or ""
                ).strip()
                docs.append(
                    {
                        "dte_type_code": raw_type,
                        "doc_type": raw_type,
                        "dte_version_code": "received",
                        "folio": folio[:40],
                        "identification_number": rut[:40],
                        "company_name": company_name[:220],
                        "counterpart_rut": rut[:40],
                        "counterpart_name": company_name[:220],
                        "issued_at": issued_at.isoformat() if issued_at else None,
                        "net_amount": _money(_find_col(row, "Neto", "Monto neto")),
                        "exempt_amount": _money(_find_col(row, "Exento")),
                        "tax_amount": _money(_find_col(row, "IVA", "Monto IVA")),
                        "total_amount": amount,
                        "branch": "",
                        "payment_status_code": "pending_payment",
                        "has_concepts": False,
                        "concepts": {},
                        "pms_class": "FNS Contabilidad",
                        "pms_ref": "",
                        "raw": {
                            **row,
                            "source_report": f"{module}/{action}",
                            "doc_type": raw_type,
                        },
                    }
                )
            if docs:
                return docs

        # Fallback: no supplier ledger in FNS Contabilidad for this property.
        return []
