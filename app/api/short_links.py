"""Public redirect for short map links used in Instagram captions."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from app.services.short_links import resolve_short_link

router = APIRouter(tags=["short-links"])


@router.get("/g/{code}")
def open_short_link(code: str) -> RedirectResponse:
    if not re.fullmatch(r"[a-z0-9]{4,8}", code):
        raise HTTPException(status_code=404, detail="No encontrado")
    target = resolve_short_link(code)
    if not target:
        raise HTTPException(status_code=404, detail="No encontrado")
    return RedirectResponse(target, status_code=302)
