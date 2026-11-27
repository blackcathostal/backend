"""Fetch utility debts (Enel / Aguas Andinas) via Servipag into received Track docs.

Account identifiers and Servipag company/category ids live in
`app/data/utility_bill_accounts.json` (not hardcoded maps in call sites).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from app.core.config import BASE_DIR

logger = logging.getLogger(__name__)

CATALOG_PATH = BASE_DIR / "app" / "data" / "utility_bill_accounts.json"
SERVIPAG_QUERY_URL = "https://portal.servipag.com/portal/bill/v3/query/"


def load_utility_bill_accounts() -> list[dict[str, Any]]:
    if not CATALOG_PATH.exists():
        return []
    try:
        rows = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("utility bill catalog unreadable: %s", exc)
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("active", True)]


def _parse_cl_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _bill_to_received_doc(account: dict[str, Any], bill: dict[str, Any]) -> dict[str, Any] | None:
    total = float(bill.get("totalAmount") or bill.get("total_amount") or 0)
    if abs(total) < 0.5:
        return None
    folio = str(
        bill.get("documentNumber")
        or bill.get("document_number")
        or account.get("client_identifier")
        or ""
    ).strip()
    if not folio:
        folio = f"UTIL-{account.get('code')}-{date.today().strftime('%Y%m')}"
    issued_at = _parse_cl_date(bill.get("expirationDate") or bill.get("expiration_date"))
    if issued_at is None:
        issued_at = date.today()
    supplier_rut = str(account.get("supplier_rut") or "").strip()
    company_name = str(
        (bill.get("company") or {}).get("name")
        if isinstance(bill.get("company"), dict)
        else ""
    ).strip() or str(account.get("company_name") or "").strip()
    return {
        "folio": folio[:40],
        "identification_number": supplier_rut,
        "company_name": company_name,
        "issued_at": issued_at,
        "total_amount": total,
        "dte_type_code": str(account.get("dte_type_code") or "39"),
        "source": "servipag",
        "pms_class": "Servipag",
        "pms_ref": str(account.get("client_identifier") or "")[:120],
        "accounting_account_code": str(account.get("accounting_account_code") or "").strip(),
        "payment_status_code": "pending_payment",
        "raw": {
            "utility_code": account.get("code"),
            "client_identifier": account.get("client_identifier"),
            "servipag": bill,
        },
    }


def _query_servipag_http(
    *,
    company_id: int,
    category_id: int,
    client_identifier: str,
    timeout: float = 45.0,
) -> dict[str, Any] | None:
    """Try Servipag bill API with a plain HTTP client (may be blocked by Cloudflare)."""
    if company_id <= 0 or not client_identifier:
        return None
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/plain, */*",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "origin": "https://portal.servipag.com",
        "referer": "https://portal.servipag.com/paymentexpress",
    }
    payload = {
        "bill": {
            "company": {"id": int(company_id)},
            "category": {"id": int(category_id)} if category_id > 0 else {"id": 0},
            "type": "standard",
            "metaData": [{"name": "identifier", "value": str(client_identifier)}],
        }
    }
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        # Warm portal cookies if possible.
        try:
            client.get("https://portal.servipag.com/paymentexpress")
        except httpx.HTTPError:
            pass
        create = client.post(SERVIPAG_QUERY_URL, json=payload)
        if create.status_code >= 400 or "Just a moment" in create.text[:200]:
            return None
        try:
            created = create.json()
        except json.JSONDecodeError:
            return None
        query_id = (
            (created.get("data") or {}).get("queryId")
            if isinstance(created.get("data"), dict)
            else None
        )
        if not query_id:
            return None
        for _ in range(12):
            poll = client.get(f"{SERVIPAG_QUERY_URL}{query_id}/lastcall/false")
            if poll.status_code >= 400:
                return None
            try:
                body = poll.json()
            except json.JSONDecodeError:
                return None
            rows = body.get("data") if isinstance(body, dict) else None
            if isinstance(rows, list) and rows:
                row = rows[0]
                if int(row.get("queryStatus") or 0) == 1 and float(row.get("totalAmount") or 0) > 0:
                    return row
                if int(row.get("queryStatus") or 0) == 1:
                    return row
            time.sleep(1.0)
    return None


def _query_servipag_browser(
    *,
    company_id: int,
    category_id: int,
    client_identifier: str,
    portal_url: str,
) -> dict[str, Any] | None:
    """Fallback: Selenium + CDP against Servipag (when Chrome is available)."""
    try:
        import undetected_chromedriver as uc
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import Select
    except ImportError:
        return None

    opts = uc.ChromeOptions()
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--lang=es-CL")
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    driver = None
    try:
        driver = uc.Chrome(options=opts, headless=True)
        driver.set_page_load_timeout(120)
        driver.execute_cdp_cmd("Network.enable", {})
        driver.get(portal_url or "https://portal.servipag.com/paymentexpress")
        time.sleep(6)
        # Prefer the payment card controls when present.
        try:
            select_el = driver.find_element(By.ID, "card-lib-selectCompany-change")
            Select(select_el).select_by_value(str(company_id))
            time.sleep(0.8)
            ident = driver.find_element(By.ID, "card-lib-identifier-change")
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'}); arguments[0].focus(); arguments[0].click();",
                ident,
            )
            driver.execute_cdp_cmd("Input.insertText", {"text": str(client_identifier)})
            time.sleep(0.3)
            btn = driver.find_element(By.ID, "card-lib-btnContinue-click")
            ActionChains(driver).move_to_element(btn).click().perform()
            time.sleep(10)
        except Exception as exc:  # noqa: BLE001
            logger.info("servipag UI fill failed, trying fetch in page: %s", exc)
            # In-page fetch with session cookies after portal load.
            result = driver.execute_async_script(
                """
                const companyId = arguments[0];
                const categoryId = arguments[1];
                const client = arguments[2];
                const done = arguments[arguments.length - 1];
                (async () => {
                  try {
                    const createResp = await fetch('https://portal.servipag.com/portal/bill/v3/query/', {
                      method: 'POST',
                      credentials: 'include',
                      headers: {'content-type':'application/json','accept':'application/json, text/plain, */*'},
                      body: JSON.stringify({
                        bill: {
                          company: { id: companyId },
                          category: { id: categoryId || 0 },
                          type: 'standard',
                          metaData: [{ name: 'identifier', value: client }]
                        }
                      })
                    });
                    const created = await createResp.json();
                    const queryId = created && created.data && created.data.queryId;
                    if (!queryId) { done({error:'no_query'}); return; }
                    for (let i = 0; i < 12; i++) {
                      const poll = await fetch(
                        `https://portal.servipag.com/portal/bill/v3/query/${queryId}/lastcall/false`,
                        { credentials: 'include', headers: {'accept':'application/json, text/plain, */*'} }
                      );
                      const body = await poll.json();
                      const row = body && Array.isArray(body.data) ? body.data[0] : null;
                      if (row && Number(row.queryStatus) === 1) { done({bill: row}); return; }
                      await new Promise(r => setTimeout(r, 1000));
                    }
                    done({error:'timeout'});
                  } catch (err) {
                    done({error: String(err)});
                  }
                })();
                """,
                int(company_id),
                int(category_id or 0),
                str(client_identifier),
            )
            if isinstance(result, dict) and isinstance(result.get("bill"), dict):
                return result["bill"]
            return None

        # Read last successful bill body from performance log.
        bill = None
        for entry in driver.get_log("performance"):
            try:
                msg = json.loads(entry["message"])["message"]
            except Exception:
                continue
            if msg.get("method") != "Network.responseReceived":
                continue
            url = (msg.get("params") or {}).get("response", {}).get("url") or ""
            if "/portal/bill/v3/query/" not in url or url.rstrip("/").endswith("/query"):
                continue
            rid = msg.get("params", {}).get("requestId")
            if not rid:
                continue
            try:
                body = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": rid})
                raw = body.get("body") or ""
                parsed = json.loads(raw)
            except Exception:
                continue
            rows = parsed.get("data") if isinstance(parsed, dict) else None
            if isinstance(rows, list) and rows and float(rows[0].get("totalAmount") or 0) > 0:
                bill = rows[0]
        return bill
    except Exception as exc:  # noqa: BLE001
        logger.warning("servipag browser scrape failed: %s", exc)
        return None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def _client_identifier_candidates(account: dict[str, Any]) -> list[str]:
    values: list[str] = []
    primary = str(account.get("client_identifier") or "").strip()
    if primary:
        values.append(primary)
    alts = account.get("client_identifier_alts") or []
    if isinstance(alts, list):
        for item in alts:
            text = str(item or "").strip()
            if text and text not in values:
                values.append(text)
    return values


def _query_aguas_andinas_site(client_identifier: str) -> dict[str, Any] | None:
    """Fallback debt lookup on aguasandinas.cl when Servipag reports zero."""
    try:
        import re

        import undetected_chromedriver as uc
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError:
        return None

    client = re.sub(r"[^0-9]", "", str(client_identifier or ""))
    if not client:
        return None
    driver = None
    try:
        opts = uc.ChromeOptions()
        opts.add_argument("--window-size=1400,1000")
        opts.add_argument("--lang=es-CL")
        driver = uc.Chrome(options=opts, headless=True)
        driver.set_page_load_timeout(120)
        driver.get("https://www.aguasandinas.cl/web/aguasandinas/pagar-mi-cuenta")
        WebDriverWait(driver, 60).until(
            lambda d: d.find_elements(By.ID, "buscador_cuenta")
        )
        time.sleep(1)
        driver.execute_script(
            """
            const client = arguments[0];
            const radio = document.getElementById('busqueda_n_cuenta');
            if (radio) radio.click();
            const field = document.getElementById('buscador_cuenta');
            field.value = client;
            const form = document.getElementById('buscar_cuenta');
            if (form) form.submit();
            """,
            client,
        )
        WebDriverWait(driver, 45).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "#tabla_cuentas input.radio, #radio1")
        )
        time.sleep(1)
        driver.execute_script(
            """
            const radio = document.querySelector('#tabla_cuentas input.radio, #radio1');
            if (radio) radio.click();
            const btn = document.querySelector('button[type=submit], input[type=submit], #btnContinuar');
            if (btn) btn.click();
            """
        )
        time.sleep(6)
        html = driver.page_source
        money = re.findall(r"\$\s*([\d\.]+)", html)
        amounts = []
        for raw in money:
            try:
                amounts.append(int(raw.replace(".", "")))
            except ValueError:
                continue
        total = max(amounts) if amounts else 0
        if total <= 0:
            return None
        return {
            "documentNumber": f"AGUAS-{client}",
            "expirationDate": date.today().strftime("%d/%m/%Y"),
            "totalAmount": total,
            "company": {"name": "Aguas Andinas"},
            "documentType": "Boleta",
            "queryStatus": 1,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("aguasandinas site scrape failed: %s", exc)
        return None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def fetch_utility_bill(account: dict[str, Any]) -> dict[str, Any] | None:
    company_id = int(account.get("servipag_company_id") or 0)
    category_id = int(account.get("servipag_category_id") or 0)
    portal_url = str(account.get("servipag_url") or "").strip()
    last_bill: dict[str, Any] | None = None
    for client in _client_identifier_candidates(account):
        bill = _query_servipag_http(
            company_id=company_id,
            category_id=category_id,
            client_identifier=client,
        )
        if bill is None:
            bill = _query_servipag_browser(
                company_id=company_id,
                category_id=category_id,
                client_identifier=client,
                portal_url=portal_url,
            )
        if not isinstance(bill, dict):
            continue
        last_bill = bill
        if float(bill.get("totalAmount") or 0) > 0.5:
            return _bill_to_received_doc(account, bill)

    code = str(account.get("code") or "").strip().lower()
    if code == "aguas_andinas":
        for client in _client_identifier_candidates(account):
            site_bill = _query_aguas_andinas_site(client)
            if site_bill is not None:
                return _bill_to_received_doc(account, site_bill)

    if last_bill is not None:
        # Keep a successful "no debt" response out of Track (amount 0).
        return _bill_to_received_doc(account, last_bill)
    return None


def fetch_utility_received_documents() -> tuple[list[dict[str, Any]], str]:
    """Return (docs, warning) for active utility accounts."""
    docs: list[dict[str, Any]] = []
    warnings: list[str] = []
    for account in load_utility_bill_accounts():
        code = str(account.get("code") or account.get("company_name") or "utility")
        try:
            doc = fetch_utility_bill(account)
        except Exception as exc:  # noqa: BLE001
            logger.exception("utility bill fetch failed for %s", code)
            warnings.append(f"{code}: {exc}")
            continue
        if doc is None:
            warnings.append(f"{code}: sin monto en Servipag")
            continue
        docs.append(doc)
    warning = " · ".join(warnings)
    return docs, warning
