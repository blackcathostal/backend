from __future__ import annotations

import json
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.accounting_track import AccountingTrackDocuments
from app.models.users import Users
from app.schemas.accounting_track import (
    AccountingTrackDocumentOut,
    AccountingTrackListResult,
    AccountingTrackRefreshResult,
)
from app.services.facturacion_client import FacturacionClient, FacturacionError
from app.services.facturacion_web import FacturacionWebClient
from app.services.fns_accounting import classify_document, fetch_pms_production

router = APIRouter(prefix="/accounting-track", tags=["accounting-track"])


def _extract_pdf_token(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    token = str(payload.get("pdf_data") or "").strip()
    if token:
        return token
    nested = payload.get("raw")
    if isinstance(nested, dict):
        return str(nested.get("pdf_data") or "").strip()
    return ""


def _has_pdf(row: AccountingTrackDocuments) -> bool:
    raw = row.raw_json or ""
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "pdf_data" in raw
    return bool(_extract_pdf_token(payload))


def _pdf_token(row: AccountingTrackDocuments) -> str:
    raw = row.raw_json or ""
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    return _extract_pdf_token(payload)


def _serialize_doc(row: AccountingTrackDocuments) -> AccountingTrackDocumentOut:
    item = AccountingTrackDocumentOut.model_validate(row)
    item.has_pdf = _has_pdf(row)
    return item


def _parse_bound(value: str | None, *, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Fecha inválida (usa YYYY-MM-DD)") from exc


def _default_range() -> tuple[date, date]:
    """Default window: from the 1st day two months ago through today."""
    today = date.today()
    year = today.year
    month = today.month - 2
    while month <= 0:
        month += 12
        year -= 1
    start = date(year, month, 1)
    return start, today


def _upsert_docs(
    db: Session,
    docs: list[dict],
    *,
    source: str,
    pms_rows: list[dict] | None = None,
    match_fns: bool = True,
) -> tuple[int, int]:
    upserted = 0
    classified = 0
    for item in docs:
        direction = str(item.get("direction") or "").strip().lower()
        if direction not in {"emitida", "recibida"}:
            continue
        doc_type = str(item.get("doc_type") or "").strip()
        folio = str(item.get("folio") or "").strip()
        counterpart_rut = str(item.get("counterpart_rut") or "").strip()
        if not folio and not counterpart_rut:
            continue

        issued_raw = item.get("issued_at")
        issued_at = None
        if isinstance(issued_raw, date):
            issued_at = issued_raw
        elif issued_raw:
            try:
                issued_at = datetime.strptime(str(issued_raw)[:10], "%Y-%m-%d").date()
            except ValueError:
                issued_at = None

        if match_fns:
            class_info = classify_document(item, pms_rows)
            if class_info.get("pms_class") and class_info["pms_class"] != "Sin match":
                classified += 1
        else:
            class_info = {"pms_class": "—", "pms_ref": ""}

        row = (
            db.query(AccountingTrackDocuments)
            .filter(
                AccountingTrackDocuments.direction == direction,
                AccountingTrackDocuments.doc_type == doc_type,
                AccountingTrackDocuments.folio == folio,
                AccountingTrackDocuments.counterpart_rut == counterpart_rut,
            )
            .first()
        )
        if row is None:
            row = AccountingTrackDocuments(
                direction=direction,
                doc_type=doc_type,
                folio=folio,
                counterpart_rut=counterpart_rut,
            )
            db.add(row)

        row.counterpart_name = str(item.get("counterpart_name") or "")[:220]
        row.issued_at = issued_at
        row.net_amount = float(item.get("net_amount") or 0)
        row.exempt_amount = float(item.get("exempt_amount") or 0)
        row.tax_amount = float(item.get("tax_amount") or 0)
        row.total_amount = float(item.get("total_amount") or 0)
        row.branch = str(item.get("branch") or "")[:120]
        row.pms_class = str(class_info.get("pms_class") or "Sin match")[:80]
        row.pms_ref = str(class_info.get("pms_ref") or "")[:120]
        row.source = source
        try:
            row.raw_json = json.dumps(item.get("raw") or item, ensure_ascii=False, default=str)
        except TypeError:
            row.raw_json = None
        upserted += 1

    db.commit()
    return upserted, classified


def _list_direction(
    db: Session,
    *,
    direction: str,
    date_from: date,
    date_to: date,
) -> AccountingTrackListResult:
    query = db.query(AccountingTrackDocuments).filter(
        AccountingTrackDocuments.direction == direction
    )
    query = query.filter(
        AccountingTrackDocuments.issued_at.isnot(None),
        AccountingTrackDocuments.issued_at >= date_from,
        AccountingTrackDocuments.issued_at <= date_to,
    )
    rows = query.order_by(
        AccountingTrackDocuments.issued_at.desc(),
        AccountingTrackDocuments.id.desc(),
    ).all()
    return AccountingTrackListResult(
        items=[_serialize_doc(row) for row in rows],
        total=len(rows),
        from_date=date_from,
        to_date=date_to,
    )


@router.get("/emitidas", response_model=AccountingTrackListResult)
def list_emitidas(
    from_date: str | None = None,
    to_date: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> AccountingTrackListResult:
    start, end = _default_range()
    return _list_direction(
        db,
        direction="emitida",
        date_from=_parse_bound(from_date, fallback=start),
        date_to=_parse_bound(to_date, fallback=end),
    )


@router.get("/recibidas", response_model=AccountingTrackListResult)
def list_recibidas(
    from_date: str | None = None,
    to_date: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> AccountingTrackListResult:
    start, end = _default_range()
    return _list_direction(
        db,
        direction="recibida",
        date_from=_parse_bound(from_date, fallback=start),
        date_to=_parse_bound(to_date, fallback=end),
    )


def _persist_pdf_token(db: Session, row: AccountingTrackDocuments, token: str) -> None:
    token = (token or "").strip()
    if not token:
        return
    payload: dict[str, object] = {}
    raw = row.raw_json or ""
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                payload = loaded
        except json.JSONDecodeError:
            payload = {}
    payload["pdf_data"] = token
    row.raw_json = json.dumps(payload, ensure_ascii=False, default=str)
    db.add(row)
    db.commit()
    db.refresh(row)


@router.get("/documents/{doc_id}/pdf")
def download_document_pdf(
    doc_id: int,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> Response:
    row = db.query(AccountingTrackDocuments).filter(AccountingTrackDocuments.id == doc_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    token = _pdf_token(row)
    try:
        with FacturacionWebClient() as web:
            if not token:
                token = web.find_pdf_data(
                    folio=row.folio or "",
                    rut=row.counterpart_rut or "",
                    issued_at=row.issued_at,
                )
                if token:
                    _persist_pdf_token(db, row, token)
            if not token:
                raise HTTPException(
                    status_code=404,
                    detail="No encontré el PDF en facturacion.cl para este folio.",
                )
            pdf_bytes = web.download_pdf(token)
    except HTTPException:
        raise
    except FacturacionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    filename = f"DTE_{row.folio or doc_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/refresh", response_model=AccountingTrackRefreshResult)
async def refresh_track(
    direction: str = "emitida",
    from_date: str | None = None,
    to_date: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> AccountingTrackRefreshResult:
    direction = (direction or "emitida").strip().lower()
    if direction not in {"emitida", "recibida"}:
        raise HTTPException(status_code=400, detail="direction debe ser emitida o recibida")

    start, end = _default_range()
    date_from = _parse_bound(from_date, fallback=start)
    date_to = _parse_bound(to_date, fallback=end)

    warning = ""
    docs: list[dict] = []
    source = "api"
    pms_rows: list[dict] = []

    try:
        if direction == "recibida":
            # Libro de compras: no SOAP contratado → scrape Plano. Sin cruce FNS.
            source = "web"
            with FacturacionWebClient() as web:
                docs = web.fetch_recibidas(date_from, date_to)
        else:
            client = FacturacionClient()
            client.login()
            docs = client.fetch_emitidas(date_from, date_to)
            pms_rows = await fetch_pms_production(date_from, date_to)
    except FacturacionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    upserted, classified = _upsert_docs(
        db,
        docs,
        source=source,
        pms_rows=pms_rows,
        match_fns=direction == "emitida",
    )
    return AccountingTrackRefreshResult(
        direction=direction,
        fetched=len(docs),
        upserted=upserted,
        classified=classified,
        warning=warning,
    )
