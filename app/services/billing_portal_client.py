"""Billing portal (facturacion.cl) DTE REST + SOAP sales-ledger client."""
from __future__ import annotations

import base64
import csv
import io
import re
from datetime import date, datetime, timedelta
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from app.core.config import settings

SOAP_URL = "https://wss.facturacion.cl/WSDS/wsplano.asmx"


class BillingPortalError(Exception):
    """Raised when billing portal credentials or API calls fail."""


def _b64(value: str) -> str:
    return base64.b64encode((value or "").encode("utf-8")).decode("ascii")


def _parse_amount(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("$", "").replace(" ", "")
    if not text:
        return 0.0
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        # Chilean thousands separators: 153.205 → 153205
        text = text.replace(".", "")
    try:
        return float(text)
    except ValueError:
        return 0.0


def _compact_key(value: object) -> str:
    """Normalize ledger headers so 'Monto Total' / 'montoneto' match the same pick()."""
    return re.sub(r"[^0-9a-z]", "", str(value or "").strip().lower())


def _derive_net_tax_from_total(
    total: float,
    *,
    doc_type: object = "",
    net_amount: float = 0.0,
    exempt_amount: float = 0.0,
    tax_amount: float = 0.0,
) -> tuple[float, float, float]:
    """
    facturacion.cl HTML ledger often exposes only TOTAL (no NETO/IVA columns).
    Reuse FNS Cobros IVA split so Neto/subtotal and IVA are filled from total.
    """
    from app.services.tax_documents_fetch import _split_cl_iva

    return _split_cl_iva(
        total,
        doc_type=doc_type,
        net_amount=net_amount,
        exempt_amount=exempt_amount,
        tax_amount=tax_amount,
    )


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


class BillingPortalClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        user: str | None = None,
        rut: str | None = None,
        password: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = (base_url or settings.billing_portal_base_url or "").rstrip("/")
        self.user = (user if user is not None else settings.billing_portal_user or "").strip()
        self.rut = (rut if rut is not None else settings.billing_portal_rut or "").strip()
        self.password = (password if password is not None else settings.billing_portal_password or "").strip()
        self.timeout = timeout
        self._token: str | None = None

    def require_credentials(self) -> None:
        env_label = "PROD" if settings.billing_portal_is_prod else "DES"
        missing = [
            name
            for name, value in (
                ("BILLING_PORTAL_BASE_URL", self.base_url),
                ("BILLING_PORTAL_USER", self.user),
                (f"BILLING_PORTAL_{env_label}_RUT", self.rut),
                (f"BILLING_PORTAL_{env_label}_PASSWORD", self.password),
            )
            if not value
        ]
        if missing:
            raise BillingPortalError(
                "Faltan credenciales de facturacion.cl: " + ", ".join(missing)
            )

    def login(self) -> str:
        self.require_credentials()
        url = f"{self.base_url}/login"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    url,
                    json={
                        "usuario": self.user,
                        "rut": self.rut,
                        "clave": self.password,
                    },
                )
        except httpx.HTTPError as exc:
            raise BillingPortalError(f"No se pudo conectar a facturacion.cl: {exc}") from exc

        if response.status_code >= 400:
            detail = (response.text or "").strip()[:200] or f"HTTP {response.status_code}"
            raise BillingPortalError(f"Login facturacion.cl falló: {detail}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise BillingPortalError("Respuesta de login facturacion.cl inválida") from exc

        token = str(payload.get("token") or "").strip()
        if not token:
            raise BillingPortalError("facturacion.cl no devolvió token de acceso")
        self._token = token
        return token

    @property
    def token(self) -> str | None:
        return self._token

    def _soap_login_xml(self) -> str:
        self.require_credentials()
        return f"""
      <login>
        <Usuario>{_b64(self.user)}</Usuario>
        <Rut>{_b64(self.rut)}</Rut>
        <Clave>{_b64(self.password)}</Clave>
        <Puerto>{_b64("0")}</Puerto>
      </login>
        """

    def _soap_call(self, method: str, inner_xml: str) -> str:
        envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
 xmlns:xsd="http://www.w3.org/2001/XMLSchema"
 xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <{method} xmlns="http://tempuri.org/">
      {inner_xml}
    </{method}>
  </soap:Body>
</soap:Envelope>"""
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"http://tempuri.org/{method}"',
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    SOAP_URL,
                    content=envelope.encode("utf-8"),
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise BillingPortalError(f"SOAP facturacion.cl falló: {exc}") from exc

        text = response.text or ""
        if response.status_code >= 400 or "soap:Fault" in text or "Fault>" in text:
            fault = ""
            match = re.search(r"<faultstring>(.*?)</faultstring>", text, re.I | re.S)
            if match:
                fault = match.group(1).strip()
            fault_l = fault.lower()
            if "servicio adicional" in fault_l or "contraten" in fault_l or "object reference" in fault_l:
                raise BillingPortalError(
                    "ConsultaLibroVentas no está contratado en facturacion.cl. "
                    "Pediles activar el servicio adicional de libro de ventas/boletas "
                    "para poder usar Actualizar en Track."
                )
            raise BillingPortalError(
                fault or "Error SOAP en facturacion.cl al consultar el libro de ventas."
            )
        return text

    def _extract_soap_result(self, xml_text: str, result_tag: str) -> str:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise BillingPortalError("Respuesta SOAP inválida") from exc
        for node in root.iter():
            if node.tag.endswith(result_tag):
                return (node.text or "").strip()
        return ""

    @staticmethod
    def resolve_dte_type_code(raw_type: object, *, book_type: str | None = None) -> str:
        """Map SII/billing-portal labels to dte_types.code (resolved to id in API via DB)."""
        text = str(raw_type or "").strip().lower()
        digits = "".join(ch for ch in text if ch.isdigit())
        if digits.startswith("61") or digits == "61" or "61" in digits:
            return "61"
        if digits.startswith("34") or digits == "34" or "34" in digits:
            return "34"
        if digits.startswith("39") or digits == "39" or "39" in digits:
            return "39"
        if digits.startswith("33") or digits == "33" or "33" in digits:
            return "33"
        if "nota de credito" in text or "nota de crédito" in text or "credit note" in text:
            return "61"
        if "exenta" in text or "exempt" in text:
            return "34"
        if "boleta" in text or "receipt" in text or (book_type or "").strip().upper() == "B":
            return "39"
        if "factura" in text or "invoice" in text or (book_type or "").strip().upper() == "V":
            return "33"
        return "33"

    @staticmethod
    def resolve_dte_type_id(raw_type: object, *, book_type: str | None = None) -> int:
        """Legacy helper: returns SII type code as int; prefer resolve_dte_type_code."""
        return int(BillingPortalClient.resolve_dte_type_code(raw_type, book_type=book_type))

    @staticmethod
    def resolve_dte_version_code(direction: str | None) -> str:
        value = str(direction or "").strip().lower()
        if value in {"recibida", "received", "2"}:
            return "received"
        return "issued"

    def _normalize_doc(
        self,
        raw: dict[str, Any],
        *,
        direction: str,
        book_type: str | None = None,
    ) -> dict[str, Any]:
        keys = {_compact_key(k): v for k, v in raw.items() if _compact_key(k)}

        def pick(*names: str) -> Any:
            for name in names:
                key = _compact_key(name)
                if key in keys:
                    return keys[key]
            return None

        issued_at = _parse_date(
            pick("fecha", "fechadocumento", "fecha_documento", "fechaemision", "fecha_emision")
        )
        raw_type = pick("tipodte", "tipo_dte", "tipo", "tipo documento", "tipodocumento", "documento") or ""
        dte_type_code = self.resolve_dte_type_code(raw_type, book_type=book_type)
        dte_version_code = self.resolve_dte_version_code(direction)
        identification_number = str(
            pick(
                "rut",
                "rutcliente",
                "rut_cliente",
                "rutreceptor",
                "rut_receptor",
                "rut proveedor",
                "rutproveedor",
            )
            or ""
        ).strip()
        company_name = str(
            pick(
                "razon",
                "razonsocial",
                "razon_social",
                "nombre",
                "cliente",
                "receptor",
                "proveedor",
            )
            or ""
        ).strip()
        if not identification_number:
            # Recover RUT embedded in the name/cell text from portal exports.
            rut_match = re.search(
                r"\b(\d{1,2}(?:\.\d{3}){2}-[\dkK]|\d{7,8}-[\dkK])\b",
                f"{identification_number} {company_name} {pick('documento') or ''}",
                flags=re.I,
            )
            if rut_match:
                identification_number = rut_match.group(1).strip()
        total_amount = _parse_amount(
            pick("montototal", "monto_total", "valortotal", "total", "importe")
        )
        net_amount = _parse_amount(
            pick("montoneto", "monto_neto", "valorneto", "neto", "subtotal")
        )
        exempt_amount = _parse_amount(
            pick("montoexento", "monto_exento", "valorexento", "exento")
        )
        tax_amount = _parse_amount(pick("montoiva", "monto_iva", "valoriva", "iva"))
        # Portal HTML grid usually only has TOTAL; back out Neto/IVA at 19%.
        net_amount, exempt_amount, tax_amount = _derive_net_tax_from_total(
            total_amount,
            doc_type=raw_type or dte_type_code,
            net_amount=net_amount,
            exempt_amount=exempt_amount,
            tax_amount=tax_amount,
        )
        return {
            "dte_type_code": dte_type_code,
            "dte_version_code": dte_version_code,
            # Provisional numeric hints; API overwrites ids from catalog tables
            "dte_type_id": int(dte_type_code) if dte_type_code.isdigit() else None,
            "dte_version_id": 2 if dte_version_code == "received" else 1,
            "folio": str(pick("folio", "nro", "numero", "n°", "nº") or "").strip(),
            "identification_number": identification_number,
            "company_name": company_name,
            # Legacy aliases for classify/PMS helpers
            "counterpart_rut": identification_number,
            "counterpart_name": company_name,
            "issued_at": issued_at.isoformat() if issued_at else None,
            "net_amount": net_amount,
            "exempt_amount": exempt_amount,
            "tax_amount": tax_amount,
            "total_amount": total_amount,
            "branch": str(pick("sucursal", "branch") or "").strip(),
            "raw": raw,
        }

    def _parse_ledger_payload(self, payload: str, *, direction: str) -> list[dict[str, Any]]:
        text = (payload or "").strip()
        if not text:
            return []

        # Sometimes the SOAP result itself is Base64 XML/CSV.
        try:
            decoded = base64.b64decode(text).decode("utf-8", errors="ignore").strip()
            if decoded:
                text = decoded
        except Exception:
            pass

        if text.startswith("<"):
            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                root = None
            if root is not None:
                rows: list[dict[str, Any]] = []
                candidates = [
                    node
                    for node in root.iter()
                    if node is not root and len(list(node)) and all(
                        (child.text or "").strip() or len(list(child)) == 0 for child in list(node)
                    )
                ]
                # Prefer repeating sibling groups with folio-like children.
                for node in root.iter():
                    children = list(node)
                    if len(children) < 2:
                        continue
                    if not any(
                        (c.tag or "").lower().endswith(("folio", "tipodte", "montoneto", "montototal"))
                        or (c.tag or "").split("}")[-1].lower()
                        in {"folio", "tipodte", "montoneto", "montototal"}
                        for c in children
                    ):
                        continue
                    raw = {
                        (c.tag or "").split("}")[-1]: (c.text or "").strip()
                        for c in children
                    }
                    if raw.get("Folio") or raw.get("folio") or raw.get("TipoDTE") or raw.get("tipodte"):
                        rows.append(
                            self._normalize_doc(raw, direction=direction, book_type=None)
                        )
                if rows:
                    return rows

        # CSV / semicolon exports
        sample = text.splitlines()[:5]
        delimiter = ";" if any(";" in line for line in sample) else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        rows = []
        for item in reader:
            if not any((v or "").strip() for v in item.values()):
                continue
            rows.append(self._normalize_doc(item, direction=direction))
        return rows

    def query_sales_ledger(self, issued_on: date, book_type: str = "B") -> list[dict[str, Any]]:
        """Issued sales ledger: book_type B=receipts, V=invoices (SOAP add-on)."""
        tipo = (book_type or "B").strip().upper()
        if tipo not in {"B", "V"}:
            raise BillingPortalError("tipolibro debe ser B (boletas) o V (ventas)")
        inner = (
            self._soap_login_xml()
            + f"<fechadocumento>{_b64(issued_on.isoformat())}</fechadocumento>"
            + f"<tipolibro>{_b64(tipo)}</tipolibro>"
        )
        xml_text = self._soap_call("ConsultaLibroVentas", inner)
        payload = self._extract_soap_result(xml_text, "ConsultaLibroVentasResult")
        docs = self._parse_ledger_payload(payload, direction="issued")
        for doc in docs:
            doc["dte_type_id"] = self.resolve_dte_type_id(
                doc.get("dte_type_code") or doc.get("dte_type_id"),
                book_type=tipo,
            )
        return docs

    def fetch_issued(
        self,
        date_from: date,
        date_to: date,
        *,
        book_types: tuple[str, ...] = ("V", "B"),
    ) -> list[dict[str, Any]]:
        """
        Issued sales ledger via SOAP ConsultaLibroVentas (day by day).
        book_types: V=facturas/ventas, B=boletas.
        Prefer BillingPortalWebClient.fetch_issued when the web ledger is available.
        """
        if date_to < date_from:
            raise BillingPortalError("Rango de fechas inválido")
        types = tuple(
            str(item or "").strip().upper()
            for item in (book_types or ("V", "B"))
            if str(item or "").strip().upper() in {"V", "B"}
        ) or ("V", "B")
        docs: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        cursor = date_from
        while cursor <= date_to:
            for book in types:
                try:
                    batch = self.query_sales_ledger(cursor, book_type=book)
                except BillingPortalError:
                    raise
                for doc in batch:
                    key = (
                        str(doc.get("dte_type_code") or ""),
                        str(doc.get("folio") or "").strip(),
                    )
                    if not key[1] or key in seen:
                        continue
                    seen.add(key)
                    docs.append(doc)
            cursor = cursor + timedelta(days=1)
        return docs

    def parse_ledger_file(
        self,
        content: bytes,
        *,
        direction: str,
        filename: str = "",
    ) -> list[dict[str, Any]]:
        """Parse Excel/CSV ledger export from the billing portal web UI."""
        name = (filename or "").lower()
        if name.endswith((".xlsx", ".xls")):
            try:
                import openpyxl
            except ImportError as exc:
                raise BillingPortalError("Falta openpyxl para leer Excel") from exc
            workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
            sheet = workbook.active
            rows_iter = sheet.iter_rows(values_only=True)
            try:
                headers = [str(h or "").strip() for h in next(rows_iter)]
            except StopIteration:
                return []
            docs: list[dict[str, Any]] = []
            for values in rows_iter:
                raw = {
                    headers[i]: values[i]
                    for i in range(min(len(headers), len(values)))
                    if headers[i]
                }
                if not any(v not in (None, "") for v in raw.values()):
                    continue
                docs.append(self._normalize_doc(raw, direction=direction))
            return docs

        text = content.decode("utf-8-sig", errors="ignore")
        return self._parse_ledger_payload(text, direction=direction)
