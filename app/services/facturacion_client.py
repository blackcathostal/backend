"""facturacion.cl DTE REST + SOAP libro client."""
from __future__ import annotations

import base64
import csv
import io
import re
from datetime import date, datetime
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from app.core.config import settings

SOAP_URL = "https://wss.facturacion.cl/WSDS/wsplano.asmx"


class FacturacionError(Exception):
    """Raised when facturacion.cl credentials or API calls fail."""


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


class FacturacionClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        usuario: str | None = None,
        rut: str | None = None,
        clave: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = (base_url or settings.facturacion_base_url or "").rstrip("/")
        self.usuario = (usuario if usuario is not None else settings.facturacion_user or "").strip()
        self.rut = (rut if rut is not None else settings.facturacion_rut or "").strip()
        self.clave = (clave if clave is not None else settings.facturacion_clave or "").strip()
        self.timeout = timeout
        self._token: str | None = None

    def require_credentials(self) -> None:
        env_label = "PROD" if settings.facturacion_is_prod else "DES"
        missing = [
            name
            for name, value in (
                ("FACTURACION_BASE_URL", self.base_url),
                ("FACTURACION_USER", self.usuario),
                (f"FACTURACION_{env_label}_RUT", self.rut),
                (f"FACTURACION_{env_label}_CLAVE", self.clave),
            )
            if not value
        ]
        if missing:
            raise FacturacionError(
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
                        "usuario": self.usuario,
                        "rut": self.rut,
                        "clave": self.clave,
                    },
                )
        except httpx.HTTPError as exc:
            raise FacturacionError(f"No se pudo conectar a facturacion.cl: {exc}") from exc

        if response.status_code >= 400:
            detail = (response.text or "").strip()[:200] or f"HTTP {response.status_code}"
            raise FacturacionError(f"Login facturacion.cl falló: {detail}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise FacturacionError("Respuesta de login facturacion.cl inválida") from exc

        token = str(payload.get("token") or "").strip()
        if not token:
            raise FacturacionError("facturacion.cl no devolvió token de acceso")
        self._token = token
        return token

    @property
    def token(self) -> str | None:
        return self._token

    def _soap_login_xml(self) -> str:
        self.require_credentials()
        return f"""
      <login>
        <Usuario>{_b64(self.usuario)}</Usuario>
        <Rut>{_b64(self.rut)}</Rut>
        <Clave>{_b64(self.clave)}</Clave>
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
            raise FacturacionError(f"SOAP facturacion.cl falló: {exc}") from exc

        text = response.text or ""
        if response.status_code >= 400 or "soap:Fault" in text or "Fault>" in text:
            fault = ""
            match = re.search(r"<faultstring>(.*?)</faultstring>", text, re.I | re.S)
            if match:
                fault = match.group(1).strip()
            fault_l = fault.lower()
            if "servicio adicional" in fault_l or "contraten" in fault_l or "object reference" in fault_l:
                raise FacturacionError(
                    "ConsultaLibroVentas no está contratado en facturacion.cl. "
                    "Pediles activar el servicio adicional de libro de ventas/boletas "
                    "para poder usar Actualizar en Track."
                )
            raise FacturacionError(
                fault or "Error SOAP en facturacion.cl al consultar el libro de ventas."
            )
        return text

    def _extract_soap_result(self, xml_text: str, result_tag: str) -> str:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise FacturacionError("Respuesta SOAP inválida") from exc
        for node in root.iter():
            if node.tag.endswith(result_tag):
                return (node.text or "").strip()
        return ""

    def _normalize_doc(self, raw: dict[str, Any], *, direction: str) -> dict[str, Any]:
        keys = {str(k).strip().lower(): v for k, v in raw.items()}

        def pick(*names: str) -> Any:
            for name in names:
                if name.lower() in keys:
                    return keys[name.lower()]
            return None

        issued_at = _parse_date(
            pick("fecha", "fechadocumento", "fecha_documento", "fechaemision", "fecha_emision")
        )
        return {
            "direction": direction,
            "doc_type": str(
                pick("tipodte", "tipo_dte", "tipo", "tipo documento", "tipodocumento") or ""
            ).strip(),
            "folio": str(pick("folio", "nro", "numero", "n°", "nº") or "").strip(),
            "counterpart_rut": str(
                pick("rut", "rutcliente", "rut_cliente", "rut proveedor", "rutproveedor") or ""
            ).strip(),
            "counterpart_name": str(
                pick(
                    "razon",
                    "razonsocial",
                    "razon_social",
                    "nombre",
                    "cliente",
                    "proveedor",
                )
                or ""
            ).strip(),
            "issued_at": issued_at.isoformat() if issued_at else None,
            "net_amount": _parse_amount(pick("montoneto", "monto_neto", "neto")),
            "exempt_amount": _parse_amount(pick("montoexento", "monto_exento", "exento")),
            "tax_amount": _parse_amount(pick("montoiva", "monto_iva", "iva")),
            "total_amount": _parse_amount(pick("montototal", "monto_total", "total")),
            "branch": str(pick("sucursal", "branch") or "").strip(),
            "raw": raw,
        }

    def _parse_libro_payload(self, payload: str, *, direction: str) -> list[dict[str, Any]]:
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
                        rows.append(self._normalize_doc(raw, direction=direction))
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

    def consulta_libro_ventas(self, issued_on: date, tipolibro: str = "B") -> list[dict[str, Any]]:
        """Emitidas: tipolibro B=boletas, V=facturas/otros (servicio adicional SOAP)."""
        tipo = (tipolibro or "B").strip().upper()
        if tipo not in {"B", "V"}:
            raise FacturacionError("tipolibro debe ser B (boletas) o V (ventas)")
        inner = (
            self._soap_login_xml()
            + f"<fechadocumento>{_b64(issued_on.isoformat())}</fechadocumento>"
            + f"<tipolibro>{_b64(tipo)}</tipolibro>"
        )
        xml_text = self._soap_call("ConsultaLibroVentas", inner)
        payload = self._extract_soap_result(xml_text, "ConsultaLibroVentasResult")
        return self._parse_libro_payload(payload, direction="emitida")

    def fetch_emitidas(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        """Pull emitidas month-by-month via SOAP (B + V)."""
        if date_to < date_from:
            raise FacturacionError("Rango de fechas inválido")
        docs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        cursor = date(date_from.year, date_from.month, 1)
        while cursor <= date_to:
            for tipolibro in ("B", "V"):
                try:
                    batch = self.consulta_libro_ventas(cursor, tipolibro=tipolibro)
                except FacturacionError:
                    raise
                for doc in batch:
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
                    seen.add(key)
                    docs.append(doc)
            if cursor.month == 12:
                cursor = date(cursor.year + 1, 1, 1)
            else:
                cursor = date(cursor.year, cursor.month + 1, 1)
        return docs

    def parse_libro_file(
        self,
        content: bytes,
        *,
        direction: str,
        filename: str = "",
    ) -> list[dict[str, Any]]:
        """Parse Excel/CSV libro export from facturacion.cl web UI."""
        name = (filename or "").lower()
        if name.endswith((".xlsx", ".xls")):
            try:
                import openpyxl
            except ImportError as exc:
                raise FacturacionError("Falta openpyxl para leer Excel") from exc
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
        return self._parse_libro_payload(text, direction=direction)
