from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException, Request

from app.core.config import settings

logger = logging.getLogger(__name__)

VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"
_missing_secret_warned = False


def client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    if request.client and request.client.host:
        return request.client.host
    return ""


def verify_recaptcha(token: str, action: str, remote_ip: str = "") -> None:
    secret = (settings.recaptcha_secret_key or "").strip()
    if not secret:
        global _missing_secret_warned
        if not _missing_secret_warned:
            logger.warning("RECAPTCHA_SECRET_KEY is empty; public form spam check is disabled")
            _missing_secret_warned = True
        return

    if not (token or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Verificación anti-spam incompleta. Recarga la página e inténtalo de nuevo.",
        )

    try:
        response = httpx.post(
            VERIFY_URL,
            data={
                "secret": secret,
                "response": token.strip(),
                "remoteip": remote_ip,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("reCAPTCHA verify failed: %s", exc)
        raise HTTPException(
            status_code=400,
            detail="No pudimos validar el envío. Inténtalo de nuevo.",
        ) from exc

    if not payload.get("success"):
        logger.info("reCAPTCHA rejected: %s", payload.get("error-codes"))
        raise HTTPException(
            status_code=400,
            detail="No pudimos verificar que eres una persona. Inténtalo de nuevo.",
        )

    expected_action = (action or "").strip()
    got_action = (payload.get("action") or "").strip()
    if expected_action and got_action and got_action != expected_action:
        raise HTTPException(
            status_code=400,
            detail="Verificación anti-spam inválida.",
        )

    hostname = (payload.get("hostname") or "").strip().lower()
    allowed = {item.strip().lower() for item in settings.recaptcha_allowed_hosts if item.strip()}
    if hostname and allowed and hostname not in allowed:
        logger.info("reCAPTCHA hostname rejected: %s", hostname)
        raise HTTPException(
            status_code=400,
            detail="Verificación anti-spam inválida.",
        )

    try:
        score = float(payload.get("score"))
    except (TypeError, ValueError):
        score = 0.0
    if score < settings.recaptcha_min_score:
        logger.info("reCAPTCHA low score: %s action=%s", score, got_action)
        raise HTTPException(
            status_code=400,
            detail="El envío fue bloqueado por seguridad. Inténtalo más tarde.",
        )
