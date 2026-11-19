"""FNS PMS Contabilidad helpers (API service, not web portal login)."""
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

# Contabilidad endpoints from PMS API catalog (POST + Basic Auth).
CONTABILIDAD_PATHS = (
    "/v1/contabilidad/facturacion.php",
    "/v1/contabilidad/cobros.php",
    "/v1/contabilidad/produccion.php",
)


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


def _as_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _auth() -> tuple[str, str] | None:
    user = (settings.fns_pms_user or "").strip()
    password = (settings.fns_pms_password or "").strip()
    if not user or not password:
        return None
    return user, password


def _service_base_url() -> str:
    # API PMS service host (not the interactive web app login flow).
    return (settings.fns_pms_base_url or "https://adminchile1.fnsmanager.com").rstrip("/")


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


def _movement_amount(movement: dict[str, Any]) -> float:
    total = 0.0
    for side in ("credito", "debito"):
        bucket = movement.get(side)
        if not isinstance(bucket, dict):
            continue
        for entry in bucket.values():
            if not isinstance(entry, dict):
                continue
            amount = abs(_parse_amount(entry.get("importe")))
            if amount:
                total = max(total, amount)
    if total:
        return total
    return abs(
        _parse_amount(
            movement.get("total")
            or movement.get("monto")
            or movement.get("importe")
            or movement.get("amount")
        )
    )


def _movement_labels(movement: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for key in (
        "origen",
        "fuente",
        "factura_tipo",
        "documento",
        "fp_codigo",
        "tipocuenta",
        "auxiliar_operativo",
    ):
        value = str(movement.get(key) or "").strip()
        if value:
            labels.append(value)
    for side in ("credito", "debito"):
        bucket = movement.get(side)
        if not isinstance(bucket, dict):
            continue
        for entry in bucket.values():
            if not isinstance(entry, dict):
                continue
            for key in ("descripcion", "cc_nombre", "cuenta"):
                value = str(entry.get(key) or "").strip()
                if value:
                    labels.append(value)
    return labels


def _flatten_contabilidad_payload(payload: dict[str, Any], *, source: str) -> list[dict[str, Any]]:
    movimientos = payload.get("movimientos")
    terceros = payload.get("terceros") if isinstance(payload.get("terceros"), dict) else {}
    rows: list[dict[str, Any]] = []
    if not isinstance(movimientos, dict):
        return rows

    for key, movement in movimientos.items():
        if not isinstance(movement, dict):
            continue
        cif = str(movement.get("cif") or "").strip()
        third = terceros.get(cif) if isinstance(terceros.get(cif), dict) else {}
        labels = _movement_labels(movement)
        if isinstance(third, dict):
            for field in ("nombre", "nombrecompleto"):
                value = str(third.get(field) or "").strip()
                if value:
                    labels.append(value)
        rows.append(
            {
                "id": str(movement.get("identificador") or key),
                "folio": str(
                    movement.get("factura_codigo")
                    or movement.get("documento")
                    or movement.get("auxiliar_operativo")
                    or ""
                ),
                "reserva": str(movement.get("auxiliar_operativo") or ""),
                "booking_id": str(movement.get("auxiliar_operativo") or ""),
                "fecha": movement.get("fecha"),
                "total": _movement_amount(movement),
                "monto": _movement_amount(movement),
                "importe": _movement_amount(movement),
                "concepto": " ".join(labels),
                "descripcion": " ".join(labels),
                "nombre": str(
                    (third or {}).get("nombrecompleto")
                    or (third or {}).get("nombre")
                    or ""
                ),
                "tipo": str(movement.get("factura_tipo") or movement.get("fuente") or source),
                "centro_coste": " ".join(
                    str((entry or {}).get("cc_nombre") or "")
                    for side in ("credito", "debito")
                    for entry in ((movement.get(side) or {}) if isinstance(movement.get(side), dict) else {}).values()
                    if isinstance(entry, dict)
                ),
                "cif": cif,
                "fuente": source,
                "raw": movement,
            }
        )
    return rows


async def fetch_pms_production(date_from: date, date_to: date) -> list[dict[str, Any]]:
    """Pull Contabilidad from FNS PMS API service (POST + Basic Auth)."""
    auth = _auth()
    if not auth:
        logger.warning("FNS PMS credentials missing; Contabilidad skipped")
        return []

    hotel = _as_int(settings.fns_hotel_pms_id or settings.fns_hotel_id)
    pms_id = _as_int(settings.fns_pms_id)
    if hotel is None:
        logger.warning("FNS alojamiento_id missing; Contabilidad skipped")
        return []

    filter_body: dict[str, Any] = {
        "alojamiento_id": hotel,
        "start_date": date_from.isoformat(),
        "end_date": date_to.isoformat(),
    }
    if pms_id is not None:
        filter_body["pms_id"] = pms_id

    body = {"filter": filter_body}
    base = _service_base_url()
    rows: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True, auth=auth) as client:
        for path in CONTABILIDAD_PATHS:
            url = f"{base}{path}"
            try:
                response = await client.post(url, json=body)
            except Exception as exc:  # noqa: BLE001
                logger.warning("FNS Contabilidad POST failed %s: %s", url, exc)
                continue

            if response.status_code >= 400:
                snippet = (response.text or "").strip()[:180] or "<empty body>"
                logger.warning(
                    "FNS Contabilidad %s -> %s %s (service host %s; Basic Auth POST)",
                    path,
                    response.status_code,
                    snippet,
                    base,
                )
                continue

            try:
                payload = response.json()
            except ValueError:
                logger.warning("FNS Contabilidad %s returned non-JSON", path)
                continue

            if not isinstance(payload, dict):
                continue

            batch = _flatten_contabilidad_payload(payload, source=path.split("/")[-1])
            if batch:
                rows.extend(batch)
                # Prefer first successful Contabilidad source with data.
                break

    return rows


def classify_document(
    doc: dict[str, Any],
    pms_rows: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Return pms_class + pms_ref for a normalized DTE dict."""
    text_class = classify_from_text(
        str(doc.get("company_name") or doc.get("counterpart_name") or ""),
        str(doc.get("dte_type_code") or doc.get("doc_type") or ""),
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
