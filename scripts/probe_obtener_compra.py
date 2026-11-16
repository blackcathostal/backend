from __future__ import annotations

import base64
import re
from datetime import date

import httpx

from app.core.config import settings


def b64(value: str) -> str:
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def main() -> None:
    ns = "http://api.facturacion.com/"
    url = "https://api.facturacion.com/WSFacturacion/Compra"
    periodo = date.today().strftime("%Y-%m")
    trials = [
        ("des-b64", settings.facturacion_des_rut, settings.facturacion_des_clave, True),
        ("des-plain", settings.facturacion_des_rut, settings.facturacion_des_clave, False),
        ("prod-b64", settings.facturacion_prod_rut, settings.facturacion_prod_clave, True),
        ("prod-plain", settings.facturacion_prod_rut, settings.facturacion_prod_clave, False),
    ]
    for label, rut, clave, encoded in trials:
        if not rut or not clave:
            continue
        rr = b64(rut) if encoded else rut
        cc = b64(clave) if encoded else clave
        pp = b64(periodo) if encoded else periodo
        envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:api="{ns}">
  <soapenv:Header/>
  <soapenv:Body>
    <api:obtenerCompra>
      <login>
        <Rut>{rr}</Rut>
        <Clave>{cc}</Clave>
      </login>
      <periodo>{pp}</periodo>
    </api:obtenerCompra>
  </soapenv:Body>
</soapenv:Envelope>"""
        response = httpx.post(
            url,
            content=envelope.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'},
            timeout=45.0,
        )
        text = " ".join((response.text or "").split())
        fault = re.search(r"<faultstring>(.*?)</faultstring>", text, re.I)
        msg = re.search(r"<Mensaje>(.*?)</Mensaje>", text, re.I)
        print(
            label,
            response.status_code,
            "fault=" + (fault.group(1) if fault else "-"),
            "msg=" + (msg.group(1) if msg else "-"),
            text[:400],
        )


if __name__ == "__main__":
    main()
