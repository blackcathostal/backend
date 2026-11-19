"""FNSrooms web extranet session (adminchile1 login + optional email IP validation)."""
from __future__ import annotations

import email
import imaplib
import json
import logging
import re
import time
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

COOKIE_CACHE = settings.uploads_dir / "cache" / "fns_web_cookies.json"
AUTH_PROBE_PATHS = ("/es/Contabilidad", "/es/Reservas")
TOKEN_URL_RE = re.compile(
    r"https://adminchile1\.fnsmanager\.com/\?token=[0-9_]+",
    flags=re.I,
)
EMAIL_GATE_HINTS = (
    "correo",
    "e-mail",
    "email",
    "enviado",
    "envia",
    "validaci",
    "autoriz",
    "30 d",
    "bandeja",
)


class FnsWebError(Exception):
    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.details = details


class FnsWebClient:
    def __init__(
        self,
        *,
        user: str | None = None,
        password: str | None = None,
        base_url: str | None = None,
        timeout: float = 45.0,
        imap_timeout_sec: int = 180,
    ) -> None:
        self.user = (user if user is not None else settings.fns_web_user or "").strip()
        self.password = (
            password if password is not None else settings.fns_web_password or ""
        ).strip()
        self.base_url = (
            base_url if base_url is not None else settings.fns_web_base_url or ""
        ).rstrip("/") or "https://adminchile1.fnsmanager.com"
        self.timeout = timeout
        self.imap_timeout_sec = max(30, int(imap_timeout_sec))
        self._client: httpx.Client | None = None

    def require_credentials(self) -> None:
        missing = [
            name
            for name, value in (
                ("FNS_WEB_USER", self.user),
                ("FNS_WEB_PASSWORD", self.password),
            )
            if not value
        ]
        if missing:
            raise FnsWebError(
                "Faltan credenciales web de FNS: " + ", ".join(missing)
            )

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Origin": self.base_url,
                    "Referer": f"{self.base_url}/",
                },
            )
            self._load_cookies()
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._save_cookies()
            self._client.close()
            self._client = None

    def __enter__(self) -> "FnsWebClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _cookie_path(self) -> Path:
        path = COOKIE_CACHE
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _load_cookies(self) -> None:
        client = self._client
        if client is None:
            return
        path = self._cookie_path()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(payload, dict):
            client.cookies.update({str(k): str(v) for k, v in payload.items()})

    def _save_cookies(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            self._cookie_path().write_text(
                json.dumps(dict(client.cookies), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Could not persist FNS web cookies: %s", exc)

    @staticmethod
    def _hidden(html: str, name: str) -> str:
        match = re.search(
            rf'name=["\']{re.escape(name)}["\'][^>]*value=["\']([^"\']*)["\']',
            html,
            flags=re.I,
        )
        return match.group(1) if match else ""

    @classmethod
    def _is_authenticated_html(cls, html: str) -> bool:
        low = html.lower()
        user_id = cls._hidden(html, "usuario_id").strip()
        estoy = cls._hidden(html, "estoy").strip().lower()
        if user_id and estoy not in {"", "entrada"}:
            return True
        if "cerrar ses" in low and "member_login" not in low:
            return True
        return False

    @classmethod
    def _looks_like_email_gate(cls, html: str) -> bool:
        low = html.lower()
        mensaje = " ".join(
            [
                cls._hidden(html, "mensaje"),
                cls._hidden(html, "alert"),
                cls._hidden(html, "error"),
            ]
        ).lower()
        blob = f"{mensaje} {low}"
        if any(hint in blob for hint in EMAIL_GATE_HINTS) and (
            "correo" in blob or "mail" in blob or "enviad" in blob
        ):
            return True
        # Still on login without session markers after ComprobarUser.
        if "member_login" in low or cls._hidden(html, "estoy").lower() == "entrada":
            if "comprobaruser" in cls._hidden(html, "accion").lower():
                return True
        return False

    def _get(self, path: str) -> httpx.Response:
        client = self._ensure_client()
        return client.get(urljoin(self.base_url + "/", path.lstrip("/")))

    def _post_login(self) -> httpx.Response:
        client = self._ensure_client()
        client.get(f"{self.base_url}/")
        payload = {
            "login": self.user,
            "rpassword": self.password,
            "accion": "ComprobarUser",
            "width": "1920",
            "height": "1080",
            "estoy": "Entrada",
        }
        return client.post(f"{self.base_url}/es/Entrada", data=payload)

    def probe_authenticated(self) -> bool:
        for path in AUTH_PROBE_PATHS:
            try:
                response = self._get(path)
            except httpx.HTTPError as exc:
                logger.info("FNS probe %s failed: %s", path, exc)
                continue
            if response.status_code == 200 and self._is_authenticated_html(response.text):
                return True
        return False

    def login(self) -> httpx.Response:
        self.require_credentials()
        response = self._post_login()
        self._save_cookies()
        return response

    @staticmethod
    def _decode_mime(value: str | None) -> str:
        if not value:
            return ""
        parts: list[str] = []
        for chunk, charset in decode_header(value):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(str(chunk))
        return "".join(parts)

    @staticmethod
    def _message_body(msg: email.message.Message) -> str:
        if msg.is_multipart():
            chunks: list[str] = []
            for part in msg.walk():
                if part.get_content_type() in {"text/plain", "text/html"}:
                    raw = part.get_payload(decode=True) or b""
                    chunks.append(
                        raw.decode(
                            part.get_content_charset() or "utf-8",
                            errors="replace",
                        )
                    )
            return "\n".join(chunks)
        raw = msg.get_payload(decode=True) or b""
        return raw.decode(msg.get_content_charset() or "utf-8", errors="replace")

    def _imap_ready(self) -> bool:
        user = (settings.fns_imap_user or "").strip()
        password = (settings.fns_imap_password or "").strip()
        return bool(user and password)

    def _imap_settings(self) -> tuple[str, int, str, str]:
        host = (settings.fns_imap_host or "imap.gmail.com").strip()
        port = int(settings.fns_imap_port or 993)
        user = (settings.fns_imap_user or "").strip()
        password = (settings.fns_imap_password or "").strip()
        if not user or not password:
            raise FnsWebError(
                "FNS pide validación por correo, pero faltan FNS_IMAP_USER / FNS_IMAP_PASSWORD"
            )
        return host, port, user, password

    def _find_validation_token_url(self, *, since_ts: float) -> str:
        """Poll IMAP for the FNS validation link.

        Gmail IMAP SEARCH with SUBJECT \"Validaci\" returns 0 hits (accent/encoding),
        even when the mail is already in INBOX. Search by FROM and filter in Python.
        """
        host, port, user, password = self._imap_settings()
        deadline = time.time() + self.imap_timeout_sec
        seen: set[str] = set()
        # Prefer newest matching mail; allow a few minutes of clock skew.
        min_ts = since_ts - 120
        while time.time() < deadline:
            mail = imaplib.IMAP4_SSL(host, port)
            try:
                mail.login(user, password)
                mail.select("INBOX")
                # Do NOT filter SUBJECT in IMAP — accented "Validación" never matches on Gmail.
                typ, data = mail.search(None, '(FROM "fnsrooms.com")')
                ids = data[0].split() if data and data[0] else []
                for num in reversed(ids[-40:]):
                    typ, msgdata = mail.fetch(num, "(RFC822)")
                    if not msgdata or not msgdata[0]:
                        continue
                    msg = email.message_from_bytes(msgdata[0][1])
                    subject = self._decode_mime(msg.get("Subject")).lower()
                    if "valid" not in subject and "acceso" not in subject:
                        continue
                    date_hdr = msg.get("Date") or ""
                    try:
                        msg_dt = parsedate_to_datetime(date_hdr)
                        msg_ts = msg_dt.timestamp()
                        if msg_ts + 10 < min_ts:
                            continue
                    except Exception:
                        pass
                    body = self._message_body(msg)
                    # HTML may repeat the same href; take first fresh token URL.
                    for link in TOKEN_URL_RE.findall(body):
                        if link not in seen:
                            seen.add(link)
                            logger.info("FNS validation token found in mail subject=%s", subject[:80])
                            return link
            finally:
                try:
                    mail.logout()
                except Exception:
                    pass
            time.sleep(4)
        return ""

    def _approve_email_validation(self, *, since_ts: float) -> str:
        token_url = self._find_validation_token_url(since_ts=since_ts)
        if not token_url:
            raise FnsWebError(
                "FNS pidió validación por correo: el mail puede haber llegado, "
                "pero no encontré el link de token a tiempo por IMAP. "
                "Reintentá Actualizar (no abras el link desde tu PC: valida la IP del servidor)."
            )
        client = self._ensure_client()
        response = client.get(token_url)
        self._save_cookies()
        logger.info(
            "FNS validation link opened status=%s url=%s",
            response.status_code,
            response.url,
        )
        return token_url

    def ensure_session(self, *, force_login: bool = False) -> dict[str, Any]:
        """Login if needed. If FNS asks for email validation, approve via IMAP and retry."""
        self.require_credentials()
        self._ensure_client()

        if not force_login and self.probe_authenticated():
            return {"authenticated": True, "via": "cached_cookies", "email_validation": False}

        started = time.time()
        first = self.login()
        if self.probe_authenticated():
            return {
                "authenticated": True,
                "via": "password_login",
                "email_validation": False,
                "login_status": first.status_code,
            }

        # Conditional: only when password login did not open Contabilidad/Reservas.
        email_gate = self._looks_like_email_gate(first.text)
        if not self._imap_ready():
            raise FnsWebError(
                "Login FNS sin sesión activa"
                + (
                    " (parece pedir validación por correo; configurá FNS_IMAP_*)"
                    if email_gate
                    else ""
                ),
                details={"status": first.status_code, "url": str(first.url)},
            )

        logger.info(
            "FNS login without session (email_gate=%s); polling IMAP for validation token",
            email_gate,
        )
        token_url = self._approve_email_validation(since_ts=started - 90)
        second = self.login()
        if not self.probe_authenticated():
            raise FnsWebError(
                "Tras validar el correo, FNS sigue sin sesión activa",
                details={"status": second.status_code, "url": str(second.url)},
            )
        return {
            "authenticated": True,
            "via": "password_login_with_email_validation",
            "email_validation": True,
            "token_url": token_url,
            "login_status": second.status_code,
        }

    def get_html(self, path: str, *, ensure: bool = True) -> str:
        if ensure:
            self.ensure_session()
        response = self._get(path)
        if ensure and not self._is_authenticated_html(response.text):
            self.ensure_session(force_login=True)
            response = self._get(path)
        if response.status_code >= 400:
            raise FnsWebError(
                f"FNS web HTTP {response.status_code} en {path}",
                details=response.text[:300],
            )
        return response.text
