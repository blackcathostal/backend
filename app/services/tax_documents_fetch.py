"""Scrape FNS Contabilidad tax/payment documents for admin Track refresh."""
from __future__ import annotations

import json
import logging
import re
from calendar import monthrange
from datetime import date, datetime
from html import unescape
from typing import Any, Iterator

from app.core.config import settings
from app.services import dte_catalog
from app.services.extranet_web_client import ExtranetWebClient, ExtranetWebError

logger = logging.getLogger(__name__)


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


def _scale_amounts_to_target(
    amounts: dict[str, float], target: float
) -> dict[str, float]:
    """Scale account totals so they sum to target (rounding drift on largest key)."""
    raw = {str(k): float(v or 0) for k, v in amounts.items()}
    total = float(sum(raw.values()))
    target_value = float(target or 0)
    if target_value <= 0.5 or total <= 0.5:
        return {k: 0.0 for k in raw}
    if abs(total - target_value) <= 0.5:
        return raw
    scaled = {k: round(v * target_value / total) for k, v in raw.items()}
    drift = round(target_value - sum(scaled.values()))
    if drift and scaled:
        top_key = max(scaled, key=lambda key: abs(scaled[key]))
        scaled[top_key] = float(scaled[top_key] + drift)
    return scaled


# Chilean VAT rate used when Cobros/Informes only expose gross total.
_IVA_RATE = 0.19


def _is_exempt_doc_type(doc_type: object) -> bool:
    needle = _norm(str(doc_type or ""))
    return (
        "exent" in needle
        or needle in {"34", "factura exenta", "exempt invoice", "exempt"}
    )


def _split_cl_iva(
    total: float,
    *,
    doc_type: object = "",
    net_amount: float = 0.0,
    exempt_amount: float = 0.0,
    tax_amount: float = 0.0,
) -> tuple[float, float, float]:
    """
    Return (net, exempt, tax) for a Chilean DTE total.

    Prefer explicit columns when present; otherwise back out 19% IVA from total
    (FNS Cobros only publishes Importe/Total).
    """
    total = float(total or 0)
    net = float(net_amount or 0)
    exempt = float(exempt_amount or 0)
    tax = float(tax_amount or 0)
    if abs(net) > 0.5 or abs(tax) > 0.5 or abs(exempt) > 0.5:
        return net, exempt, tax
    if abs(total) < 0.5:
        return 0.0, 0.0, 0.0
    if _is_exempt_doc_type(doc_type):
        # No VAT: show the gross as Neto/subtotal and keep exempt for accounting.
        return total, total, 0.0
    # net = total / 1.19 (CLP integer-friendly); tax = remainder so they sum to total.
    sign = -1.0 if total < 0 else 1.0
    absolute = abs(total)
    net_abs = round(absolute / (1.0 + _IVA_RATE))
    tax_abs = round(absolute - net_abs)
    return sign * net_abs, 0.0, sign * tax_abs


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
        raise ExtranetWebError(
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


# FNS Cobros layout columns that are never revenue concepts (table schema, not CoA).
_NON_CONCEPT_HEADERS = frozenset(
    {
        "id",
        "fecha",
        "creado el",
        "tipo",
        "tipo documento",
        "folio",
        "n documento",
        "no documento",
        "importe",
        "total",
        "monto",
        "restante",
        "pendiente",
        "nombre",
        "cliente",
        "titular",
        "localizador",
        "forma de cobro",
        "canal",
        "origen",
        "comentario",
        "comentarios",
        "empresa",
        "moneda",
        "rut",
    }
)


def _concept_breakdown(
    row: dict[str, str],
    *,
    accounts: list | None = None,
) -> dict[str, float]:
    """Extract money columns that map to income concepts (from catalog labels when given)."""
    from app.services.accounting_concept_map import is_concept_column

    out: dict[str, float] = {}
    for key, value in row.items():
        nk = _norm(key)
        if nk in _NON_CONCEPT_HEADERS:
            continue
        if accounts is not None:
            if not is_concept_column(key, accounts):
                continue
        amount = _money(value)
        if amount:
            out[key] = amount
    return out


def _is_clp_currency(value: object) -> bool:
    currency = str(value or "").strip().upper()
    return (not currency) or currency in {"CLP", "CL$", "PESO", "PESOS", "$"}


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


class TaxDocumentsFetchClient:
    """Fetch issued/received Contabilidad documents via FNS web session."""

    def __init__(self, client: ExtranetWebClient | None = None) -> None:
        self._owns_client = client is None
        self.client = client or ExtranetWebClient()

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "TaxDocumentsFetchClient":
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
            raise ExtranetWebError(
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

    def _normalize_collection_row(self, row: dict[str, str], *, direction: str) -> dict[str, Any] | None:
        doc_type = _find_col(row, "Tipo documento", "Tipo")
        folio = _find_col(row, "Nº documento", "N° documento", "No documento", "Folio")
        folio = re.sub(r"\s+", "", folio)
        if not folio and not doc_type:
            # Payments without document still matter for account status updates by locator.
            folio = _find_col(row, "ID") or _find_col(row, "Localizador")
        if not folio:
            return None

        amount = _money(_find_col(row, "Importe", "Total", "Monto"))
        remaining_raw = str(_find_col(row, "Restante", "Pendiente") or "").strip()
        remaining_amount = _money(remaining_raw) if remaining_raw else 0.0
        concepts = _concept_breakdown(row)
        has_concepts = bool(concepts)
        # Cobros rows are payments: blank Restante usually means that payment settled.
        # Final paid/pending is refined with income-collections pending by locator.
        if remaining_raw:
            paid = remaining_amount <= 0.5 and amount > 0
        else:
            paid = amount > 0
        status_code = "paid" if paid else "pending_payment"
        # Keep FNS document type as-is; catalog resolves/creates against DB.
        raw_type = str(doc_type or "").strip()
        net_amount, exempt_amount, tax_amount = _split_cl_iva(
            amount,
            doc_type=raw_type,
            net_amount=_money(_find_col(row, "Neto", "Monto neto", "Subtotal")),
            exempt_amount=_money(_find_col(row, "Exento", "Monto exento")),
            tax_amount=_money(_find_col(row, "IVA", "Monto IVA", "Iva")),
        )
        issued_at = _parse_date(_find_col(row, "Fecha", "Creado el"))
        company_name = _find_col(row, "Nombre", "Cliente", "Titular")
        locator = _find_col(row, "Localizador")
        channel = _find_col(row, "Canal")
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
            "net_amount": net_amount,
            "exempt_amount": exempt_amount,
            "tax_amount": tax_amount,
            "total_amount": amount,
            "branch": "",
            "payment_status_code": status_code,
            "has_concepts": has_concepts,
            "concepts": concepts,
            "pms_ref": locator[:120],
            "pms_class": "FNS Collections",
            "sales_channel": channel[:80],
            "raw": {
                **row,
                "concepts": concepts,
                "payment_status_code": status_code,
                "remaining_amount": remaining_amount,
                "source_report": "collections",
                "fns_action": "Cobros",
                "doc_type": raw_type,
                "sales_channel": channel,
            },
        }

    def _income_collections_index(
        self, date_from: date, date_to: date
    ) -> tuple[dict[str, str], dict[str, float]]:
        """From FNS income-collections: locator -> guest name, locator -> pending (CLP)."""
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
            return {}, {}
        try:
            payload = json.loads(unescape(match.group(1)).replace("&quot;", '"'))
            block = payload[0] if isinstance(payload, list) else payload
            headers = (block.get("cabecera") or {}).get("1") or []
            body = block.get("cuerpo") or {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}, {}
        names: dict[str, str] = {}
        pending: dict[str, float] = {}
        for item in body.values():
            if not isinstance(item, list):
                continue
            row = {
                headers[i]: item[i] if i < len(item) else None for i in range(len(headers))
            }
            locator = re.sub(r"\s+", "", str(row.get("Localizador") or ""))
            if not locator:
                continue
            name = re.sub(r"\s+", " ", str(row.get("Datos cliente") or "")).strip()
            if name:
                names[locator] = name
            currency = str(row.get("Moneda") or "").strip().upper()
            # Track balances in CLP; USD rows are ignored for pending status.
            if currency and currency not in {"CLP", "CL$", "PESO", "PESOS", ""}:
                continue
            pending_amount = _money(row.get("Pendiente"))
            if pending_amount > float(pending.get(locator) or 0):
                pending[locator] = pending_amount
        return names, pending

    def _client_name_by_locator(
        self, date_from: date, date_to: date
    ) -> dict[str, str]:
        """Map reservation locator -> customer name from FNS income-collections."""
        names, _pending = self._income_collections_index(date_from, date_to)
        return names

    def _apply_locator_payment_status(
        self, doc: dict[str, Any], *, pending_by_locator: dict[str, float]
    ) -> None:
        """Override collections paid/pending using reservation pending from income-collections."""
        locator = re.sub(r"\s+", "", str(doc.get("pms_ref") or ""))
        if not locator:
            return
        pending_amount = float(pending_by_locator.get(locator) or 0)
        if pending_amount > 0.5:
            doc["payment_status_code"] = "pending_payment"
        else:
            # No open balance on the reservation => treat linked docs as paid.
            doc["payment_status_code"] = "paid"
        raw = dict(doc.get("raw") or {})
        raw["payment_status_code"] = doc["payment_status_code"]
        raw["reservation_pending_amount"] = pending_amount
        doc["raw"] = raw

    def _merge_issued_row(
        self,
        merged: dict[tuple[str, str], dict[str, Any]],
        doc: dict[str, Any],
        *,
        client_by_locator: dict[str, str],
        pending_by_locator: dict[str, float],
        date_from: date,
        date_to: date,
    ) -> None:
        issued = _parse_date(doc.get("issued_at"))
        if issued and (issued < date_from or issued > date_to):
            return
        locator = str(doc.get("pms_ref") or "").strip()
        ingreso_name = client_by_locator.get(locator, "")
        cobro_name = str(doc.get("company_name") or "").strip()
        # Prefer real guest name from income-collections when collections name is empty/generic.
        if ingreso_name and (
            not cobro_name or _norm(cobro_name) in {"consumos consumos", "consumos"}
        ):
            doc["company_name"] = ingreso_name
            doc["counterpart_name"] = ingreso_name
        self._apply_locator_payment_status(doc, pending_by_locator=pending_by_locator)
        key = (
            str(doc.get("doc_type") or doc.get("dte_type_code") or ""),
            str(doc.get("folio") or ""),
        )
        current = merged.get(key)
        if current is None:
            merged[key] = doc
            return
        # Same folio may appear in several payments: sum totals, keep pending if any open.
        current["total_amount"] = float(current.get("total_amount") or 0) + float(
            doc.get("total_amount") or 0
        )
        current["net_amount"] = float(current.get("net_amount") or 0) + float(
            doc.get("net_amount") or 0
        )
        current["exempt_amount"] = float(current.get("exempt_amount") or 0) + float(
            doc.get("exempt_amount") or 0
        )
        current["tax_amount"] = float(current.get("tax_amount") or 0) + float(
            doc.get("tax_amount") or 0
        )
        concepts = dict(current.get("concepts") or {})
        for name, amount in (doc.get("concepts") or {}).items():
            concepts[name] = float(concepts.get(name) or 0) + float(amount or 0)
        current["concepts"] = concepts
        current["has_concepts"] = bool(concepts)
        if (
            current.get("payment_status_code") == "pending_payment"
            or doc.get("payment_status_code") == "pending_payment"
        ):
            current["payment_status_code"] = "pending_payment"
        raw = dict(current.get("raw") or {})
        raw["concepts"] = concepts
        raw["payment_status_code"] = current["payment_status_code"]
        if doc.get("raw", {}).get("reservation_pending_amount") is not None:
            raw["reservation_pending_amount"] = doc["raw"]["reservation_pending_amount"]
        current["raw"] = raw
        if doc.get("company_name") and (
            not current.get("company_name")
            or _norm(str(current.get("company_name"))) in {"consumos consumos", "consumos"}
        ):
            current["company_name"] = doc["company_name"]
            current["counterpart_name"] = doc["company_name"]

    def _income_collections_rows(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        """Raw CLP rows from FNS income-collections report."""
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
            return []
        try:
            payload = json.loads(unescape(match.group(1)).replace("&quot;", '"'))
            block = payload[0] if isinstance(payload, list) else payload
            headers = (block.get("cabecera") or {}).get("1") or []
            body = block.get("cuerpo") or {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        rows: list[dict[str, Any]] = []
        for item in body.values():
            if not isinstance(item, list):
                continue
            row = {
                headers[i]: item[i] if i < len(item) else None for i in range(len(headers))
            }
            if not _is_clp_currency(row.get("Moneda")):
                continue
            rows.append(row)
        return rows

    def _income_collections_sales_totals(
        self, date_from: date, date_to: date
    ) -> dict[str, float]:
        """Sum sold / collected / pending / invoiced from FNS income-collections (CLP)."""
        totals = {"sold": 0.0, "collected": 0.0, "pending": 0.0, "invoiced": 0.0}
        for row in self._income_collections_rows(date_from, date_to):
            # FNS HTML column labels stay Spanish; map into English keys here.
            totals["sold"] += _money(row.get("Imp. Total"))
            totals["collected"] += _money(row.get("Cobrado"))
            totals["pending"] += _money(row.get("Pendiente"))
            totals["invoiced"] += _money(row.get("Facturado"))
        return totals

    def _iter_collection_rows(
        self, date_from: date, date_to: date
    ) -> Iterator[dict[str, str]]:
        for window_from, window_to in _month_windows(date_from, date_to):
            html = self._post_report(
                module="Informes",
                action="Cobros",
                date_from=window_from,
                date_to=window_to,
            )
            for row in _parse_html_table(html, "Informe de cobros"):
                yield row

    def _collection_concept_account_sums(
        self,
        date_from: date,
        date_to: date,
        *,
        accounts: list,
    ) -> dict[str, float]:
        """Sum Cobros concept columns mapped via accounting_accounts.concept_labels."""
        from app.services.accounting_concept_map import account_code_for_concept

        by_code: dict[str, float] = {}
        for window_from, window_to in _month_windows(date_from, date_to):
            html = self._post_report(
                module="Informes",
                action="Cobros",
                date_from=window_from,
                date_to=window_to,
            )
            rows = _parse_html_table(html, "Informe de cobros")
            for row in rows:
                concepts = _concept_breakdown(row, accounts=accounts)
                for header, amount in concepts.items():
                    code = account_code_for_concept(header, accounts)
                    if not code:
                        continue
                    by_code[code] = float(by_code.get(code) or 0) + float(amount or 0)
        return by_code

    @staticmethod
    def _parse_usd_clp_rate(html: str) -> float:
        """Read header FX like 'CL$ - US$: 900' from FNS pages."""
        match = re.search(
            r"CL\$\s*-\s*US\$\s*:\s*([\d.,]+)",
            html or "",
            flags=re.I,
        )
        if not match:
            return 0.0
        return float(_money(match.group(1)) or 0.0)

    def fetch_commission_fees_by_channel(
        self,
        date_from: date,
        date_to: date,
    ) -> dict[str, float]:
        """
        FNS Informes → Comisiones: OTA fee totals by channel (Booking, Expedia, …).

        Prefers CL$ summary rows; converts US$ fees with the page FX rate when needed.
        Returns channel label → commission fee in CLP.
        """
        if date_to < date_from:
            raise ExtranetWebError("Rango de fechas inválido")
        html = self._post_report(
            module="Informes",
            action="comisiones",
            date_from=date_from,
            date_to=date_to,
        )
        fx = self._parse_usd_clp_rate(html)
        pattern = re.compile(
            r"<td[^>]*colspan=['\"]5['\"][^>]*>\s*([^<]+?)\s*</td>\s*"
            r"<td[^>]*class=['\"]text-right['\"][^>]*>\s*<strong>(.*?)</strong>\s*</td>\s*"
            r"<td[^>]*class=['\"]text-right['\"][^>]*>\s*<strong>(.*?)</strong>\s*</td>",
            re.I | re.S,
        )
        clp_fees: dict[str, float] = {}
        usd_fees: dict[str, float] = {}
        for match in pattern.finditer(html or ""):
            channel = re.sub(r"\s+", " ", match.group(1)).strip()
            if not channel:
                continue
            fee_raw = re.sub(r"<[^>]+>", "", match.group(3) or "").strip()
            fee = float(_money(fee_raw) or 0)
            if abs(fee) <= 0.5:
                continue
            upper = fee_raw.upper()
            if "US$" in upper or "USD" in upper:
                usd_fees[channel] = float(usd_fees.get(channel, 0.0) + fee)
            else:
                # CL$ (or bare Chilean amount)
                clp_fees[channel] = float(clp_fees.get(channel, 0.0) + fee)

        out: dict[str, float] = dict(clp_fees)
        for channel, usd_fee in usd_fees.items():
            if channel in out and abs(out[channel]) > 0.5:
                # Already have a CL$ total for this channel.
                continue
            if fx > 0.5:
                out[channel] = round(usd_fee * fx)
            else:
                # No FX available; skip USD-only rows rather than invent CLP.
                continue
        return out

    def fetch_income_by_account(
        self,
        date_from: date,
        date_to: date,
        *,
        accounts: list,
    ) -> dict[str, Any]:
        """
        Legacy helper: income-collections sold total split by collections concepts.

        Prefer issued tax_documents for EERR rows; this remains for KPI fallbacks.
        """
        if date_to < date_from:
            raise ExtranetWebError("Rango de fechas inválido")
        sales = self._income_collections_sales_totals(date_from, date_to)
        by_code_raw = self._collection_concept_account_sums(
            date_from, date_to, accounts=accounts
        )
        concepts_raw_total = float(sum(float(v or 0) for v in by_code_raw.values()))
        sold = float(sales.get("sold") or 0)
        collected = float(sales.get("collected") or 0)
        pending = float(sales.get("pending") or 0)

        if sold > 0.5 and concepts_raw_total > sold + 0.5:
            by_code = _scale_amounts_to_target(by_code_raw, sold)
            concepts_total = float(sum(by_code.values()))
            scale = sold / concepts_raw_total if concepts_raw_total else 1.0
        else:
            by_code = {
                str(code): float(amount or 0) for code, amount in by_code_raw.items()
            }
            concepts_total = concepts_raw_total
            scale = 1.0

        remainder = round(max(sold - concepts_total, 0.0))
        pending_sales = round(min(max(pending, 0.0), remainder))
        unclassified_collections = round(max(remainder - pending_sales, 0.0))
        return {
            "sold": sold,
            "collected": collected,
            "pending": pending,
            "invoiced": float(sales.get("invoiced") or 0),
            "concepts_total": concepts_total,
            "concepts_raw_total": concepts_raw_total,
            "scale": float(scale),
            "pending_sales": float(pending_sales),
            "unclassified_collections": float(unclassified_collections),
            "by_account_code": by_code,
            "source": "extranet_income_collections",
        }

    def fetch_income_detail_lines(
        self,
        date_from: date,
        date_to: date,
        *,
        accounts: list,
        row_kind: str = "account",
        account_code: str = "",
    ) -> list[dict[str, Any]]:
        """
        Drill-down lines for EERR:
        - account: Cobros concept amounts mapped to account_code
        - pending: income-collections rows with pending > 0
        - collected: income-collections rows with collected > 0
        - unclassified: collections amount not covered by concept columns
        """
        from app.services.accounting_concept_map import account_code_for_concept

        kind = str(row_kind or "account").strip().lower()
        code = str(account_code or "").strip()
        lines: list[dict[str, Any]] = []

        if kind in {"pending", "collected"}:
            amount_key = "Pendiente" if kind == "pending" else "Cobrado"
            concept_label = "Pendiente" if kind == "pending" else "Cobrado"
            for row in self._income_collections_rows(date_from, date_to):
                amount = _money(row.get(amount_key))
                if amount <= 0.5:
                    continue
                locator = re.sub(r"\s+", "", str(row.get("Localizador") or ""))
                guest = re.sub(
                    r"\s+", " ", str(row.get("Datos cliente") or "")
                ).strip()
                arrival = str(row.get("Llegada") or "").strip()
                doc_blob = re.sub(
                    r"\s+", " ", str(row.get("Documento") or "")
                ).strip(" ,")
                lines.append(
                    {
                        "issued_at": arrival or None,
                        "folio": "",
                        # Empty FNS Documento ⇒ no tax doc yet (user-visible Spanish label OK).
                        "doc_type": doc_blob or "Sin documento emitido",
                        "company_name": guest,
                        "locator": locator,
                        "concept": concept_label,
                        "amount": float(amount),
                    }
                )
            return lines

        scale = 1.0
        if kind == "account":
            sales = self._income_collections_sales_totals(date_from, date_to)
            sold = float(sales.get("sold") or 0)
            by_code_raw = self._collection_concept_account_sums(
                date_from, date_to, accounts=accounts
            )
            concepts_raw_total = float(
                sum(float(v or 0) for v in by_code_raw.values())
            )
            if sold > 0.5 and concepts_raw_total > sold + 0.5:
                scale = sold / concepts_raw_total

        for row in self._iter_collection_rows(date_from, date_to):
            concepts = _concept_breakdown(row, accounts=accounts)
            concepts_sum = float(sum(float(v or 0) for v in concepts.values()))
            importe = _money(
                row.get("Importe") or row.get("Total") or row.get("Monto")
            )
            folio = re.sub(
                r"\s+",
                "",
                str(
                    row.get("Nº documento")
                    or row.get("N° documento")
                    or row.get("No documento")
                    or row.get("Folio")
                    or ""
                ),
            )
            doc_type = str(row.get("Tipo documento") or row.get("Tipo") or "").strip()
            company = str(
                row.get("Nombre") or row.get("Cliente") or row.get("Titular") or ""
            ).strip()
            locator = re.sub(r"\s+", "", str(row.get("Localizador") or ""))
            issued = _parse_date(row.get("Fecha") or row.get("Creado el"))
            issued_s = issued.isoformat() if issued else None

            if kind == "unclassified":
                gap = round(importe - concepts_sum)
                if gap <= 0.5:
                    continue
                lines.append(
                    {
                        "issued_at": issued_s,
                        "folio": folio[:40],
                        "doc_type": doc_type,
                        "company_name": company[:220],
                        "locator": locator[:120],
                        "concept": "Sin concepto",
                        "amount": float(gap),
                    }
                )
                continue

            # account drill-down (scaled to month sold when collections exceed it)
            if not code:
                continue
            for header, amount in concepts.items():
                mapped = account_code_for_concept(header, accounts)
                if mapped != code:
                    continue
                value = round(float(amount or 0) * scale)
                if abs(value) <= 0.5:
                    continue
                lines.append(
                    {
                        "issued_at": issued_s,
                        "folio": folio[:40],
                        "doc_type": doc_type,
                        "company_name": company[:220],
                        "locator": locator[:120],
                        "concept": str(header),
                        "amount": value,
                    }
                )
        return lines

    def _collection_concepts_by_locator(
        self,
        date_from: date,
        date_to: date,
        *,
        accounts: list | None = None,
    ) -> dict[str, dict[str, float]]:
        """Aggregate FNS Collections concept columns keyed by reservation locator."""
        by_locator: dict[str, dict[str, float]] = {}
        for row in self._iter_collection_rows(date_from, date_to):
            locator = re.sub(r"\s+", "", str(row.get("Localizador") or ""))
            if not locator:
                continue
            concepts = _concept_breakdown(row, accounts=accounts)
            if not concepts:
                continue
            bucket = by_locator.setdefault(locator, {})
            for header, amount in concepts.items():
                bucket[header] = float(bucket.get(header) or 0.0) + float(amount or 0)
        return by_locator

    def _normalize_income_collections_row(
        self,
        row: dict[str, Any],
        *,
        concepts: dict[str, float] | None = None,
        concept_header: str | None = None,
        amount_override: float | None = None,
        folio_suffix: str = "",
    ) -> dict[str, Any] | None:
        """
        Issued Track row from FNS income-collections reservation.

        Imp. Total = sold; Cobrado/Pendiente → paid vs pending.
        Optional Cobros concept split assigns Bar/Cafeteria/… instead of all lodging.
        """
        reservation_id = re.sub(r"\s+", "", str(row.get("ID") or ""))
        locator = re.sub(r"\s+", "", str(row.get("Localizador") or ""))
        folio_base = reservation_id or locator
        if not folio_base:
            return None
        folio = f"{folio_base}{folio_suffix}"[:40]

        total = float(_money(row.get("Imp. Total")) or 0)
        collected = float(_money(row.get("Cobrado")) or 0)
        pending = float(_money(row.get("Pendiente")) or 0)
        if abs(total) <= 0.5 and abs(collected) <= 0.5:
            return None
        sold = total if abs(total) > 0.5 else collected
        amount = float(amount_override) if amount_override is not None else sold
        if abs(amount) <= 0.5:
            return None

        arrival = _parse_date(row.get("Llegada"))
        departure = _parse_date(row.get("Salida"))
        issued_at = departure or arrival
        guest = re.sub(r"\s+", " ", str(row.get("Datos cliente") or "")).strip()
        channel = re.sub(r"\s+", " ", str(row.get("Canal") or "")).strip()
        doc_blob = re.sub(r"\s+", " ", str(row.get("Documento") or "")).strip(" ,")
        # Reservation sales: receipt when FNS lists documents, else voucher.
        # Never pass the Documento folio list as dte_type_code (e.g. "3398" contains
        # "33" and was misclassified as Factura).
        type_code = (
            dte_catalog.TYPE_CODE_RECEIPT
            if doc_blob
            else dte_catalog.TYPE_CODE_VOUCHER
        )
        # User-visible doc label (Spanish OK) — short, not the folio dump.
        doc_type_label = "Boleta" if doc_blob else "Sin documento emitido"
        status_code = (
            "paid" if pending <= 0.5 and collected > 0.5 else "pending_payment"
        )
        net_amount, exempt_amount, tax_amount = _split_cl_iva(
            amount, doc_type=doc_type_label
        )
        concept_map = {
            str(k): float(v or 0)
            for k, v in (concepts or {}).items()
            if abs(float(v or 0)) > 0.5
        }
        if concept_header and abs(amount) > 0.5:
            concept_map = {str(concept_header): float(amount)}
        return {
            "dte_type_code": type_code,
            "doc_type": doc_type_label,
            "dte_version_code": "issued",
            "folio": folio,
            "identification_number": "",
            "company_name": guest[:220],
            "counterpart_rut": "",
            "counterpart_name": guest[:220],
            "issued_at": issued_at.isoformat() if issued_at else None,
            "net_amount": net_amount,
            "exempt_amount": exempt_amount,
            "tax_amount": tax_amount,
            "total_amount": amount,
            "branch": "",
            "payment_status_code": status_code,
            "has_concepts": bool(concept_map),
            "concepts": concept_map,
            "pms_ref": locator[:120],
            "pms_class": "FNS Income Collections",
            "sales_channel": channel[:80],
            "raw": {
                **{str(k): v for k, v in row.items()},
                "concepts": concept_map,
                "payment_status_code": status_code,
                "sold_amount": sold,
                "line_amount": amount,
                "collected_amount": collected,
                "pending_amount": pending,
                "remaining_amount": pending,
                "source_report": "income_collections",
                "fns_action": "ingresoscobros",
                "doc_type": doc_type_label,
                "sales_channel": channel,
                "concept_header": concept_header or "",
            },
        }

    def fetch_issued(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        """
        Issued Track refresh: amounts from FNS Informes → Cobros (Importe).

        Each Cobros payment row becomes issued Track line(s). Concept columns
        (Alojamiento, Bar, …) are scaled to Importe so the month total matches
        the Cobros report. Income-collections only enriches pending by locator.
        """
        if date_to < date_from:
            raise ExtranetWebError("Rango de fechas inválido")

        from app.services.accounting_concept_map import load_income_accounts

        accounts: list = []
        try:
            from app.core.database import SessionLocal

            db = SessionLocal()
            try:
                from app.services.accounting_concept_map import (
                    ensure_concept_labels_from_coa,
                )

                ensure_concept_labels_from_coa(db)
                accounts = load_income_accounts(db)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 — scrape must continue without CoA
            logger.warning("Issued refresh CoA unavailable for concepts: %s", exc)

        _, pending_by_locator = self._income_collections_index(date_from, date_to)
        merged: dict[str, dict[str, Any]] = {}

        for row in self._iter_collection_rows(date_from, date_to):
            amount = float(_money(_find_col(row, "Importe", "Total", "Monto")) or 0)
            if abs(amount) <= 0.5:
                continue

            issued_at = _parse_date(_find_col(row, "Fecha", "Creado el"))
            if issued_at is not None and (issued_at < date_from or issued_at > date_to):
                continue

            concepts = _concept_breakdown(row, accounts=accounts or None)
            concept_total = float(sum(float(v or 0) for v in concepts.values()))
            lines: list[dict[str, Any]] = []

            if concepts and concept_total > 0.5:
                scaled = _scale_amounts_to_target(concepts, amount)
                for header, line_amount in scaled.items():
                    if abs(float(line_amount or 0)) <= 0.5:
                        continue
                    slug = re.sub(r"[^a-z0-9]+", "", _norm(header))[:12] or "x"
                    doc = self._normalize_collection_row(row, direction="issued")
                    if not doc:
                        continue
                    base_folio = str(doc.get("folio") or "")
                    doc["folio"] = f"{base_folio}-{slug}"[:40]
                    doc["total_amount"] = float(line_amount)
                    net_amount, exempt_amount, tax_amount = _split_cl_iva(
                        float(line_amount),
                        doc_type=doc.get("dte_type_code") or doc.get("doc_type") or "",
                    )
                    doc["net_amount"] = net_amount
                    doc["exempt_amount"] = exempt_amount
                    doc["tax_amount"] = tax_amount
                    doc["concepts"] = {str(header): float(line_amount)}
                    doc["has_concepts"] = True
                    raw = dict(doc.get("raw") or {})
                    raw["concepts"] = doc["concepts"]
                    raw["concept_header"] = str(header)
                    raw["line_amount"] = float(line_amount)
                    raw["source_report"] = "collections"
                    raw["fns_action"] = "Cobros"
                    doc["raw"] = raw
                    lines.append(doc)
            else:
                doc = self._normalize_collection_row(row, direction="issued")
                if doc:
                    lines.append(doc)

            locator = re.sub(r"\s+", "", str(_find_col(row, "Localizador") or ""))
            pending = float(pending_by_locator.get(locator) or 0)
            for doc in lines:
                if pending > 0.5:
                    doc["payment_status_code"] = "pending_payment"
                    raw = dict(doc.get("raw") or {})
                    raw["payment_status_code"] = "pending_payment"
                    raw["reservation_pending_amount"] = pending
                    doc["raw"] = raw
                folio = str(doc.get("folio") or "")
                if not folio:
                    continue
                current = merged.get(folio)
                if current is None:
                    merged[folio] = doc
                    continue
                # Same folio: keep the higher amount (avoid losing Cobros Importe).
                if float(doc.get("total_amount") or 0) > float(
                    current.get("total_amount") or 0
                ):
                    merged[folio] = doc
        return list(merged.values())

    def fetch_received(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        """
        Received Contabilidad documents from FNS (not facturacion.cl).

        FNS Contabilidad does not expose a supplier Libro de Compras like facturacion.cl.
        We reuse Cobros rows that are operationally registered without an issued tax type only
        when they carry document metadata usable as received track lines; otherwise return [].
        """
        if date_to < date_from:
            raise ExtranetWebError("Rango de fechas inválido")

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
            except ExtranetWebError:
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
                net_amount, exempt_amount, tax_amount = _split_cl_iva(
                    amount,
                    doc_type=raw_type,
                    net_amount=_money(_find_col(row, "Neto", "Monto neto", "Subtotal")),
                    exempt_amount=_money(_find_col(row, "Exento", "Monto exento")),
                    tax_amount=_money(_find_col(row, "IVA", "Monto IVA", "Iva")),
                )
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
                        "net_amount": net_amount,
                        "exempt_amount": exempt_amount,
                        "tax_amount": tax_amount,
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
