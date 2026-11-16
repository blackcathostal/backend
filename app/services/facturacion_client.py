"""facturacion.cl DTE REST client (login + helpers)."""
from __future__ import annotations

import httpx

from app.core.config import settings


class FacturacionError(Exception):
    """Raised when facturacion.cl credentials or login fail."""


class FacturacionClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        usuario: str | None = None,
        rut: str | None = None,
        clave: str | None = None,
        timeout: float = 30.0,
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
