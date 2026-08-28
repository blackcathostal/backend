from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx


class SourceFetchError(ValueError):
    pass


class _ReadableTextParser(HTMLParser):
    ignored_tags = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.image_candidates: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value for key, value in attrs}
        if tag == "meta":
            property_name = (attributes.get("property") or attributes.get("name") or "").lower()
            if property_name in {"og:image", "twitter:image", "twitter:image:src"}:
                image = attributes.get("content")
                if image:
                    self.image_candidates.append(image.strip())
        elif tag == "img":
            for attribute in ("src", "data-src", "data-lazy-src"):
                image = attributes.get(attribute)
                if image:
                    self.image_candidates.append(image.strip())
            for attribute in ("srcset", "data-srcset"):
                srcset = attributes.get(attribute)
                if srcset:
                    for item in srcset.split(","):
                        image = item.strip().split(" ", 1)[0]
                        if image:
                            self.image_candidates.append(image)
        if tag in self.ignored_tags:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.ignored_tags and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = re.sub(r"\s+", " ", data).strip()
        if not value:
            return
        if self._in_title:
            self.title_parts.append(value)
        self.text_parts.append(value)


def validate_source_url(url: str) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https":
        raise SourceFetchError("Solo se permiten fuentes HTTPS.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise SourceFetchError("La URL de la fuente no es válida.")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        raise SourceFetchError("La fuente apunta a un destino local no permitido.")

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise SourceFetchError("No se pudo resolver el dominio de la fuente.") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_unspecified
            or ip.is_multicast
        ):
            raise SourceFetchError("La fuente apunta a una red privada no permitida.")
    return value


def _extract_text(content: bytes, content_type: str, base_url: str) -> tuple[str, str, list[str]]:
    encoding_match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    encoding = encoding_match.group(1) if encoding_match else "utf-8"
    text = content.decode(encoding, errors="replace")
    if "html" in content_type.lower():
        parser = _ReadableTextParser()
        parser.feed(text)
        title = " ".join(parser.title_parts).strip()
        text = "\n".join(parser.text_parts)
    else:
        title = ""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise SourceFetchError("La fuente no contiene texto legible.")
    image_urls: list[str] = []
    if "html" in content_type.lower():
        for candidate in parser.image_candidates[:16]:
            try:
                image_url = validate_source_url(urljoin(base_url, candidate))
                if image_url not in image_urls:
                    image_urls.append(image_url)
            except SourceFetchError:
                continue
    return title[:240], text, image_urls


async def fetch_source_content(
    url: str,
    *,
    timeout_seconds: float = 15.0,
    max_bytes: int = 1_000_000,
) -> dict[str, Any]:
    current_url = validate_source_url(url)
    headers = {
        "User-Agent": "BlackCatTourismBot/1.0 (+https://blackcathostal.com)",
        "Accept": "text/html,text/plain,application/xhtml+xml",
    }

    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
        for _ in range(4):
            async with client.stream("GET", current_url, headers=headers) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise SourceFetchError("La fuente devolvió una redirección inválida.")
                    current_url = validate_source_url(urljoin(current_url, location))
                    continue
                if response.status_code >= 400:
                    raise SourceFetchError(f"La fuente respondió HTTP {response.status_code}.")
                content_type = response.headers.get("content-type", "")
                if not any(kind in content_type.lower() for kind in ("text/html", "text/plain", "application/xhtml")):
                    raise SourceFetchError("La fuente no es un documento de texto compatible.")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise SourceFetchError("La fuente supera el tamaño máximo permitido.")
                    chunks.append(chunk)
                title, text, image_urls = _extract_text(
                    b"".join(chunks), content_type, current_url
                )
                return {
                    "url": current_url,
                    "title": title,
                    "text": text[:max_bytes],
                    "image_url": image_urls[0] if image_urls else "",
                    "image_urls": image_urls,
                }

    raise SourceFetchError("La fuente tuvo demasiadas redirecciones.")


async def download_image(
    url: str,
    *,
    timeout_seconds: float = 15.0,
    max_bytes: int = 3_000_000,
) -> tuple[bytes, str]:
    current_url = validate_source_url(url)
    headers = {"User-Agent": "BlackCatTourismBot/1.0 (+https://blackcathostal.com)"}
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
        for _ in range(4):
            async with client.stream("GET", current_url, headers=headers) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise SourceFetchError("La imagen devolvió una redirección inválida.")
                    current_url = validate_source_url(urljoin(current_url, location))
                    continue
                if response.status_code >= 400:
                    raise SourceFetchError(f"La imagen respondió HTTP {response.status_code}.")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                    raise SourceFetchError("La fuente no devolvió una imagen compatible.")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise SourceFetchError("La imagen supera el tamaño máximo permitido.")
                    chunks.append(chunk)
                return b"".join(chunks), content_type
    raise SourceFetchError("La imagen tuvo demasiadas redirecciones.")
