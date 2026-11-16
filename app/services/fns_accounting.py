"""FNS PMS helpers to classify DTE amounts (hospedaje / consumos / etc.)."""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

CLASS_HOSPEDAJE = "Hospedaje"
CLASS_CONSUMOS = "Consumos"
CLASS_ESTACIONAMIENTO = "Estacionamiento"
CLASS_SERVICIOS = "Servicios"
CLASS_SIN_MATCH = "Sin match"


def _parse_amount(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _auth() -> tuple[str, str] | None:
    user = (settings.fns_pms_user or "").strip()
    password = (settings.fns_pms_password or "").strip()
    if not user or not password:
        return None
    return user, password


def classify_from_text(*parts: str) -> str | None:
    blob = " ".join(str(p or "") for p in parts).lower()
    if not blob.strip():
        return None
    if any(k in blob for k in ("hosped", "aloj", "habitacion", "habitación", "cama", "room")):
        return CLASS_HOSPEDAJE
    if any(k in blob for k in ("estacion", "parking", "garage", "cochera")):
        return CLASS_ESTACIONAMIENTO
    if any(
        k in blob
        for k in (
            "consumo",
            "bar",
            "cafeteria",
            "cafetería",
            "restaurant",
            "minibar",
            "comida",
            "bebida",
        )
    ):
        return CLASS_CONSUMOS
    if any(
        k in blob
        for k in (
            "piscina",
            "lavander",
            "tour",
            "transfer",
            "spa",
            "servicio",
            "internet",
        )
    ):
        return CLASS_SERVICIOS
    return None


async def fetch_pms_production(date_from: date, date_to: date) -> list[dict[str, Any]]:
    """Best-effort pull of PMS production/charges for classification."""
    auth = _auth()
    if not auth:
        return []

    pms = (settings.fns_pms_base_url or "").rstrip("/")
    ms = (settings.fns_ms_base_url or "").rstrip("/")
    hotel = (settings.fns_hotel_pms_id or settings.fns_hotel_id or "").strip()
    pms_id = (settings.fns_pms_id or "").strip()
    params = {
        "pms_id": pms_id,
        "hotel_id": hotel,
        "fecha_desde": date_from.isoformat(),
        "fecha_hasta": date_to.isoformat(),
        "desde": date_from.isoformat(),
        "hasta": date_to.isoformat(),
    }
    paths = [
        "/v1/contabilidad/facturacion.php",
        "/v1/contabilidad/cobros.php",
        "/v1/ventas/produccion.php",
        "/v1/ventas/produccion_centro_coste.php",
        "/api/pms/reportes/cobros",
    ]
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=40.0, follow_redirects=True, auth=auth) as client:
        for base in (pms, ms):
            if not base:
                continue
            for path in paths:
                try:
                    response = await client.get(f"{base}{path}", params=params)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("FNS GET failed %s%s: %s", base, path, exc)
                    continue
                if response.status_code >= 400:
                    continue
                try:
                    payload = response.json()
                except ValueError:
                    continue
                if isinstance(payload, list):
                    rows.extend([x for x in payload if isinstance(x, dict)])
                elif isinstance(payload, dict):
                    for key in ("data", "rows", "items", "result", "produccion", "cobros"):
                        value = payload.get(key)
                        if isinstance(value, list):
                            rows.extend([x for x in value if isinstance(x, dict)])
                            break
                if rows:
                    return rows
    return rows


def classify_document(
    doc: dict[str, Any],
    pms_rows: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Return pms_class + pms_ref for a normalized DTE dict."""
    text_class = classify_from_text(
        str(doc.get("counterpart_name") or ""),
        str(doc.get("doc_type") or ""),
        str((doc.get("raw") or {}).get("Glosa") or ""),
        str((doc.get("raw") or {}).get("glosa") or ""),
        str((doc.get("raw") or {}).get("Detalle") or ""),
        str((doc.get("raw") or {}).get("detalle") or ""),
    )
    if text_class:
        return {"pms_class": text_class, "pms_ref": "texto"}

    issued = _parse_date(doc.get("issued_at"))
    total = _parse_amount(doc.get("total_amount"))
    pms_rows = pms_rows or []
    best: dict[str, Any] | None = None
    best_score = 0.0
    for row in pms_rows:
        row_date = _parse_date(
            row.get("fecha")
            or row.get("fecha_documento")
            or row.get("date")
            or row.get("fecha_cargo")
        )
        row_total = _parse_amount(
            row.get("total")
            or row.get("monto")
            or row.get("amount")
            or row.get("importe")
        )
        score = 0.0
        if issued and row_date and issued == row_date:
            score += 2.0
        if total and row_total and abs(total - row_total) <= max(1.0, total * 0.02):
            score += 3.0
        label = classify_from_text(
            str(row.get("centro_coste") or ""),
            str(row.get("centro") or ""),
            str(row.get("concepto") or ""),
            str(row.get("descripcion") or ""),
            str(row.get("nombre") or ""),
            str(row.get("tipo") or ""),
        )
        if label:
            score += 1.5
        if score > best_score:
            best_score = score
            best = {**row, "_label": label or CLASS_HOSPEDAJE}

    if best and best_score >= 3.0:
        ref = str(
            best.get("id")
            or best.get("folio")
            or best.get("reserva")
            or best.get("booking_id")
            or ""
        )
        return {
            "pms_class": str(best.get("_label") or CLASS_HOSPEDAJE),
            "pms_ref": ref or "pms",
        }

    return {"pms_class": CLASS_SIN_MATCH, "pms_ref": ""}
