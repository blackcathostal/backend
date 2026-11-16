from __future__ import annotations

import base64
import re
from datetime import date

import httpx

from app.core.config import settings


def b64(value: str) -> str:
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def soap_call(url: str, method: str, ns: str, inner: str) -> httpx.Response:
    envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
 xmlns:xsd="http://www.w3.org/2001/XMLSchema"
 xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <{method} xmlns="{ns}">
      {inner}
    </{method}>
  </soap:Body>
</soap:Envelope>"""
    return httpx.post(
        url,
        content=envelope.encode("utf-8"),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{ns}{method}"',
        },
        timeout=45.0,
        follow_redirects=True,
    )


def show(label: str, response: httpx.Response) -> None:
    text = " ".join((response.text or "").split())
    fault = re.search(r"<faultstring>(.*?)</faultstring>", text, re.I)
    print(
        label,
        response.status_code,
        "fault=" + (fault.group(1) if fault else "-"),
        text[:350],
    )


def main() -> None:
    user = settings.facturacion_user
    rut = settings.facturacion_rut
    clave = settings.facturacion_clave
    prod_rut = settings.facturacion_prod_rut
    prod_clave = settings.facturacion_prod_clave
    print(
        "env",
        settings.facturacion_env,
        "user",
        user,
        "rut",
        rut,
        "prod_rut",
        prod_rut,
    )

    for url in (
        "https://api.facturacion.com/WSFacturacion/Compra?wsdl",
        "https://wss.facturacion.cl/WSDS/wspaneldte.asmx?wsdl",
    ):
        try:
            response = httpx.get(url, timeout=30.0, follow_redirects=True)
            ops = re.findall(r'operation name="([^"]+)"', response.text or "")
            print("WSDL", url, response.status_code, "ops", ops[:20])
        except Exception as exc:  # noqa: BLE001
            print("WSDL fail", url, exc)

    periodo = date.today().strftime("%Y-%m")
    ns = "http://tempuri.org/"

    # Panel DTE Recibidos (pending acuse, not full libro)
    panel_login = f"""
      <login>
        <Usuario>{b64(user)}</Usuario>
        <Rut>{b64(rut)}</Rut>
        <Clave>{b64(clave)}</Clave>
      </login>
      <Folio></Folio><Tipo></Tipo><RutProveedor></RutProveedor>
    """
    show(
        "ConsultaPanelDte/des",
        soap_call(
            "https://wss.facturacion.cl/WSDS/wspaneldte.asmx",
            "ConsultaPanelDte",
            ns,
            panel_login,
        ),
    )
    if prod_rut and prod_clave:
        panel_login_prod = f"""
      <login>
        <Usuario>{b64(user)}</Usuario>
        <Rut>{b64(prod_rut)}</Rut>
        <Clave>{b64(prod_clave)}</Clave>
      </login>
      <Folio></Folio><Tipo></Tipo><RutProveedor></RutProveedor>
        """
        show(
            "ConsultaPanelDte/prod",
            soap_call(
                "https://wss.facturacion.cl/WSDS/wspaneldte.asmx",
                "ConsultaPanelDte",
                ns,
                panel_login_prod,
            ),
        )

    compra_variants = [
        (
            "compra/b64-login",
            f"""
      <login>
        <Rut>{b64(rut)}</Rut>
        <Clave>{b64(clave)}</Clave>
      </login>
      <periodo>{periodo}</periodo>
            """,
        ),
        (
            "compra/plain-login",
            f"""
      <login>
        <Rut>{rut}</Rut>
        <Clave>{clave}</Clave>
      </login>
      <periodo>{periodo}</periodo>
            """,
        ),
        (
            "compra/b64-all",
            f"""
      <login>
        <Rut>{b64(rut)}</Rut>
        <Clave>{b64(clave)}</Clave>
      </login>
      <periodo>{b64(periodo)}</periodo>
            """,
        ),
    ]
    if prod_rut and prod_clave:
        compra_variants.extend(
            [
                (
                    "compra/prod-b64",
                    f"""
      <login>
        <Rut>{b64(prod_rut)}</Rut>
        <Clave>{b64(prod_clave)}</Clave>
      </login>
      <periodo>{periodo}</periodo>
                    """,
                ),
                (
                    "compra/prod-plain",
                    f"""
      <login>
        <Rut>{prod_rut}</Rut>
        <Clave>{prod_clave}</Clave>
      </login>
      <periodo>{periodo}</periodo>
                    """,
                ),
            ]
        )

    for label, inner in compra_variants:
        show(
            label,
            soap_call(
                "https://api.facturacion.com/WSFacturacion/Compra",
                "ObtenerCompra",
                ns,
                inner,
            ),
        )

    # Probe web login pages (no credentials dump)
    web_urls = [
        "https://www.facturacion.cl/",
        "https://www.facturacion.cl/plano/",
        settings.facturacion_web_test_url or "https://www.facturacion.cl/plano/",
        "https://login.facturacion.cl/",
        "https://app.facturacion.cl/",
    ]
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for url in web_urls:
            try:
                response = client.get(url)
                forms = len(re.findall(r"<form", response.text or "", re.I))
                inputs = re.findall(
                    r'<input[^>]+name=["\']([^"\']+)["\']',
                    response.text or "",
                    re.I,
                )
                print(
                    "WEB",
                    url,
                    response.status_code,
                    "forms",
                    forms,
                    "inputs",
                    inputs[:12],
                    "final",
                    str(response.url),
                )
            except Exception as exc:  # noqa: BLE001
                print("WEB fail", url, exc)


if __name__ == "__main__":
    main()
