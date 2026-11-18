from __future__ import annotations

from datetime import date

from app.services.billing_portal_web import BillingPortalWebClient


def main() -> None:
    start = date(2025, 11, 1)
    end = date(2025, 11, 30)
    with BillingPortalWebClient() as client:
        docs = client.fetch_received(start, end)
    print("docs", len(docs))
    for item in docs[:5]:
        print(item)


if __name__ == "__main__":
    main()
