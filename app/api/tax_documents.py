from __future__ import annotations

import json
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session, joinedload

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.suppliers import Suppliers
from app.models.tax_documents import DteStatuses, TaxDocuments
from app.models.users import Users
from app.schemas.tax_documents import (
    DteStatusOut,
    TaxDocumentListResult,
    TaxDocumentOut,
    TaxDocumentPatch,
    TaxDocumentRefreshResult,
)
from app.services import dte_catalog
from app.services.facturacion_client import FacturacionClient, FacturacionError
from app.services.facturacion_web import FacturacionWebClient
from app.services.fns_accounting import classify_document, fetch_pms_production

router = APIRouter(tags=["tax-documents"])


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


def _has_pdf(row: TaxDocuments) -> bool:
    raw = row.raw_json or ""
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "pdf_data" in raw
    return bool(_extract_pdf_token(payload))


def _pdf_token(row: TaxDocuments) -> str:
    raw = row.raw_json or ""
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    return _extract_pdf_token(payload)


def _resolve_type_id(db: Session, item: dict) -> int | None:
    raw = item.get("dte_type_id")
    if raw is None or raw == "":
        raw = item.get("dte_type_code") or item.get("doc_type") or ""
    return dte_catalog.type_id_from_raw(db, raw)


def _list_statuses(db: Session) -> list[DteStatusOut]:
    rows = db.query(DteStatuses).order_by(DteStatuses.id.asc()).all()
    return [DteStatusOut.model_validate(row) for row in rows]


def _supplier_name_map(db: Session, identification_numbers: list[str]) -> dict[str, str]:
    ids = sorted({str(x or "").strip() for x in identification_numbers if str(x or "").strip()})
    if not ids:
        return {}
    rows = (
        db.query(Suppliers)
        .filter(Suppliers.identification_number.in_(ids))
        .all()
    )
    return {row.identification_number: (row.company_name or "") for row in rows}


def _serialize_doc(row: TaxDocuments, *, company_name: str = "") -> TaxDocumentOut:
    item = TaxDocumentOut.model_validate(row)
    item.has_pdf = _has_pdf(row)
    item.company_name = company_name or ""
    if row.dte_type is not None:
        item.dte_type_code = row.dte_type.code or ""
        item.dte_type_name = row.dte_type.name or ""
    if row.dte_version is not None:
        item.dte_version_code = row.dte_version.code or ""
        item.dte_version_name = row.dte_version.name or ""
    if row.status is not None:
        item.status_name = row.status.name or ""
        item.status_code = row.status.code or ""
        item.status_tip_color = row.status.tip_color or "red"
    return item


def _parse_bound(value: str | None, *, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Fecha inválida (usa YYYY-MM-DD)") from exc


def _default_range() -> tuple[date, date]:
    from app.core.config import settings

    today = date.today()
    months = max(1, int(getattr(settings, "tax_documents_lookback_months", 12) or 12))
    year = today.year
    month = today.month - months
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1), today


def _upsert_supplier(db: Session, identification_number: str, company_name: str) -> bool:
    identification_number = str(identification_number or "").strip()
    if not identification_number:
        return False
    company_name = str(company_name or "").strip()[:220]
    row = (
        db.query(Suppliers)
        .filter(Suppliers.identification_number == identification_number)
        .first()
    )
    if row is None:
        db.add(
            Suppliers(
                identification_number=identification_number,
                company_name=company_name,
            )
        )
        return True
    if company_name and row.company_name != company_name:
        row.company_name = company_name
    return False


def _upsert_docs(
    db: Session,
    docs: list[dict],
    *,
    source: str,
    dte_version_id: int,
    pms_rows: list[dict] | None = None,
    match_fns: bool = True,
) -> tuple[int, int, int]:
    upserted = 0
    classified = 0
    suppliers_upserted = 0
    version_ids = dte_catalog.all_version_ids(db)
    pending_status_id = dte_catalog.pending_payment_status_id(db)

    for item in docs:
        version_id = int(item.get("dte_version_id") or dte_version_id or 0)
        if version_id not in version_ids:
            version_id = dte_version_id
        type_id = _resolve_type_id(db, item)
        folio = str(item.get("folio") or "").strip()
        identification_number = str(
            item.get("identification_number")
            or item.get("counterpart_rut")
            or ""
        ).strip()
        company_name = str(
            item.get("company_name") or item.get("counterpart_name") or ""
        ).strip()
        if not folio and not identification_number:
            continue

        if _upsert_supplier(db, identification_number, company_name):
            suppliers_upserted += 1

        issued_raw = item.get("issued_at")
        issued_at = None
        if isinstance(issued_raw, date):
            issued_at = issued_raw
        elif issued_raw:
            try:
                issued_at = datetime.strptime(str(issued_raw)[:10], "%Y-%m-%d").date()
            except ValueError:
                issued_at = None

        # Keep classify_document compatible during transition
        classify_payload = {
            **item,
            "counterpart_rut": identification_number,
            "counterpart_name": company_name,
        }
        if match_fns:
            class_info = classify_document(classify_payload, pms_rows)
            if class_info.get("pms_class") and class_info["pms_class"] != "Sin match":
                classified += 1
        else:
            class_info = {"pms_class": "—", "pms_ref": ""}

        row = (
            db.query(TaxDocuments)
            .filter(
                TaxDocuments.dte_version_id == version_id,
                TaxDocuments.dte_type_id == type_id,
                TaxDocuments.folio == folio,
                TaxDocuments.identification_number == identification_number,
            )
            .first()
        )
        is_new = row is None
        if is_new:
            row = TaxDocuments(
                dte_version_id=version_id,
                dte_type_id=type_id,
                folio=folio,
                identification_number=identification_number,
                status_id=pending_status_id,
                period="",
            )
            db.add(row)

        row.dte_version_id = version_id
        row.dte_type_id = type_id
        if is_new or row.period is None:
            row.period = ""
        if not row.status_id and pending_status_id:
            row.status_id = pending_status_id
        row.identification_number = identification_number
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
    return upserted, classified, suppliers_upserted


def _list_version(
    db: Session,
    *,
    dte_version_id: int,
    date_from: date,
    date_to: date,
) -> TaxDocumentListResult:
    query = (
        db.query(TaxDocuments)
        .options(
            joinedload(TaxDocuments.status),
            joinedload(TaxDocuments.dte_type),
            joinedload(TaxDocuments.dte_version),
        )
        .filter(TaxDocuments.dte_version_id == dte_version_id)
    )
    query = query.filter(
        TaxDocuments.issued_at.isnot(None),
        TaxDocuments.issued_at >= date_from,
        TaxDocuments.issued_at <= date_to,
    )
    rows = query.order_by(
        TaxDocuments.issued_at.desc(),
        TaxDocuments.id.desc(),
    ).all()
    names = _supplier_name_map(db, [row.identification_number for row in rows])
    return TaxDocumentListResult(
        items=[
            _serialize_doc(row, company_name=names.get(row.identification_number or "", ""))
            for row in rows
        ],
        total=len(rows),
        from_date=date_from,
        to_date=date_to,
        statuses=_list_statuses(db),
    )


@router.get("/issued", response_model=TaxDocumentListResult)
def list_issued(
    from_date: str | None = None,
    to_date: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TaxDocumentListResult:
    start, end = _default_range()
    version_id = dte_catalog.version_id_from_raw(db, dte_catalog.VERSION_CODE_ISSUED)
    return _list_version(
        db,
        dte_version_id=version_id,
        date_from=_parse_bound(from_date, fallback=start),
        date_to=_parse_bound(to_date, fallback=end),
    )


@router.get("/received", response_model=TaxDocumentListResult)
def list_received(
    from_date: str | None = None,
    to_date: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TaxDocumentListResult:
    start, end = _default_range()
    version_id = dte_catalog.version_id_from_raw(db, dte_catalog.VERSION_CODE_RECEIVED)
    return _list_version(
        db,
        dte_version_id=version_id,
        date_from=_parse_bound(from_date, fallback=start),
        date_to=_parse_bound(to_date, fallback=end),
    )


@router.get("/emitidas", response_model=TaxDocumentListResult, include_in_schema=False)
def list_issued_legacy(
    from_date: str | None = None,
    to_date: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TaxDocumentListResult:
    return list_issued(from_date=from_date, to_date=to_date, db=db, _=_)


@router.get("/recibidas", response_model=TaxDocumentListResult, include_in_schema=False)
def list_received_legacy(
    from_date: str | None = None,
    to_date: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TaxDocumentListResult:
    return list_received(from_date=from_date, to_date=to_date, db=db, _=_)


@router.get("/statuses", response_model=list[DteStatusOut])
def list_dte_statuses(
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> list[DteStatusOut]:
    return _list_statuses(db)


@router.patch("/documents/{doc_id}", response_model=TaxDocumentOut)
def patch_document(
    doc_id: int,
    payload: TaxDocumentPatch,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TaxDocumentOut:
    row = (
        db.query(TaxDocuments)
        .options(
            joinedload(TaxDocuments.status),
            joinedload(TaxDocuments.dte_type),
            joinedload(TaxDocuments.dte_version),
        )
        .filter(TaxDocuments.id == doc_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    if payload.status_id is not None:
        status = db.query(DteStatuses).filter(DteStatuses.id == payload.status_id).first()
        if status is None:
            raise HTTPException(status_code=400, detail="status_id inválido")
        row.status_id = payload.status_id

    if payload.period is not None:
        row.period = str(payload.period or "").strip()[:40]

    db.add(row)
    db.commit()
    row = (
        db.query(TaxDocuments)
        .options(
            joinedload(TaxDocuments.status),
            joinedload(TaxDocuments.dte_type),
            joinedload(TaxDocuments.dte_version),
        )
        .filter(TaxDocuments.id == doc_id)
        .first()
    )
    names = _supplier_name_map(db, [row.identification_number])
    return _serialize_doc(
        row,
        company_name=names.get(row.identification_number or "", ""),
    )


def _persist_pdf_token(db: Session, row: TaxDocuments, token: str) -> None:
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
    row = db.query(TaxDocuments).filter(TaxDocuments.id == doc_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    token = _pdf_token(row)
    try:
        with FacturacionWebClient() as web:
            if not token:
                token = web.find_pdf_data(
                    folio=row.folio or "",
                    rut=row.identification_number or "",
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


@router.post("/refresh", response_model=TaxDocumentRefreshResult)
async def refresh_tax_documents(
    direction: str = "issued",
    from_date: str | None = None,
    to_date: str | None = None,
    db: Session = Depends(get_db),
    _: Users = Depends(get_current_user),
) -> TaxDocumentRefreshResult:
    try:
        version_id = dte_catalog.version_id_from_raw(db, direction)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="version must be issued/received or 1/2") from exc

    received_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_RECEIVED)
    issued_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_ISSUED)

    start, end = _default_range()
    date_from = _parse_bound(from_date, fallback=start)
    date_to = _parse_bound(to_date, fallback=end)

    warning = ""
    docs: list[dict] = []
    source = "api"
    pms_rows: list[dict] = []

    try:
        if version_id == received_id:
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

    for doc in docs:
        doc["dte_version_id"] = version_id
        resolved_type = _resolve_type_id(db, doc)
        if resolved_type is not None:
            doc["dte_type_id"] = resolved_type

    upserted, classified, suppliers_upserted = _upsert_docs(
        db,
        docs,
        source=source,
        dte_version_id=version_id,
        pms_rows=pms_rows,
        match_fns=version_id == issued_id,
    )
    return TaxDocumentRefreshResult(
        dte_version_id=version_id,
        fetched=len(docs),
        upserted=upserted,
        classified=classified,
        suppliers_upserted=suppliers_upserted,
        warning=warning,
    )
