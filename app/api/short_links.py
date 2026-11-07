"""Public redirects: maps short links + WhatsApp landing without OG image."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.services.short_links import resolve_short_link, whatsapp_direct_url

router = APIRouter(tags=["short-links"])


@router.get("/g/{code}")
def open_short_link(code: str) -> RedirectResponse:
    if not re.fullmatch(r"[a-z0-9]{4,8}", code):
        raise HTTPException(status_code=404, detail="No encontrado")
    target = resolve_short_link(code)
    if not target:
        raise HTTPException(status_code=404, detail="No encontrado")
    return RedirectResponse(target, status_code=302)


@router.get("/w/{digits}", response_class=HTMLResponse)
def open_whatsapp_landing(digits: str) -> HTMLResponse:
    """
    Landing used in Instagram DMs instead of wa.me.

    Meta's crawler does not run JS, so it never fetches WhatsApp's OG image.
    Humans open this page and are redirected (or tap the link) to WhatsApp.
    """
    if not re.fullmatch(r"56\d{8,12}", digits):
        raise HTTPException(status_code=404, detail="No encontrado")
    target = whatsapp_direct_url(digits)
    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="robots" content="noindex,nofollow" />
  <meta property="og:title" content="Black Cat Hostal" />
  <meta property="og:description" content="Abrir WhatsApp" />
  <title>WhatsApp · Black Cat Hostal</title>
  <style>
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
           font-family: system-ui, sans-serif; background: #111; color: #f5f5f5; }}
    a {{ color: #25d366; font-size: 1.2rem; font-weight: 600; text-decoration: none; }}
  </style>
</head>
<body>
  <p><a id="go" href="{target}">Abrir WhatsApp</a></p>
  <script>
    setTimeout(function () {{
      window.location.replace({target!r});
    }}, 30);
  </script>
</body>
</html>
"""
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "public, max-age=300",
            # Help some crawlers skip rich unfurling.
            "X-Robots-Tag": "noindex, nofollow, noarchive",
        },
    )
