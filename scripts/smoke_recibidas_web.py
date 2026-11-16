from __future__ import annotations

from datetime import date

from app.services.facturacion_web import FacturacionWebClient


def main() -> None:
    start = date(2025, 11, 1)
    end = date(2025, 11, 30)
    with FacturacionWebClient() as client:
        docs = client.fetch_recibidas(start, end)
    print("docs", len(docs))
    for item in docs[:5]:
        print(item)


if __name__ == "__main__":
    main()
