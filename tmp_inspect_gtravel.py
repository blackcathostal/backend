"""Inspect Google Travel photo gallery HTML for Black Cat Hostal visitor photos."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import httpx

OUT = Path(r"c:\Users\jesus\Desktop\proyecto_blackcat\backend\tmp_gtravel")
OUT.mkdir(parents=True, exist_ok=True)

URL = (
    "https://www.google.com/travel/search?q=black%20cat%20hostal"
    "&g2lb=4965990,72471280,72560029,72573224,72647020,72686036,72803964,72882230,73064764,121529350,121738283,121762713"
    "&hl=es-419&gl=cl&ssta=1"
    "&ts=CAEaSQopEicyJTB4OTY2MmM1Yjg0NzdjZjc1YjoweDliYzJjYTMwZjgxYjZlZmYSHBIUCgcI6g8QBxgaEgcI6g8QBxgbGAEyBAgAEAAqBwoFOgNDTFA"
    "&qs=CAEyFENnc0lfOTN0d0lfR3N1R2JBUkFCOAJCCQn_bhv4MMrCm0IJCf9uG_gwysKbSAA"
    "&ap=MAC6AQZwaG90b3M&ictx=111"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-419,es;q=0.9",
}

IMG_RE = re.compile(r"https://lh[0-9]\.googleusercontent\.com/[^\s\"'<>\\]+", re.I)


def classify(url: str) -> str:
    u = url.lower()
    if "/a-/" in u or "rp-mo" in u or "-mo-ba" in u:
        return "avatar"
    if "/p/" in u:
        return "place_p"
    if "/gps-cs-s/" in u:
        return "gps_guest"
    if "/proxy/" in u:
        return "proxy"
    return "other"


def main() -> None:
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60.0) as client:
        r = client.get(URL)
        text = r.text
        (OUT / "page.html").write_text(text, encoding="utf-8", errors="ignore")
        print("status", r.status_code, "len", len(text))

        urls = []
        for m in IMG_RE.findall(text):
            u = m.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
            u = u.split('"')[0].split("'")[0].split("\\")[0]
            urls.append(u)

        # unique by base path
        uniq = {}
        for u in urls:
            key = re.sub(r"=.*$", "", u)
            uniq[key] = u

        counts = Counter(classify(u) for u in uniq.values())
        print("unique", len(uniq), dict(counts))

        # Look for AF1Qip (classic place photos) and hotel media markers
        af1 = [u for u in uniq.values() if "AF1Qip" in u or "/p/AF" in u]
        print("AF1Qip/p photos", len(af1))
        for u in af1[:20]:
            print(" P", u[:160])

        gps = [u for u in uniq.values() if "/gps-cs-s/" in u]
        print("gps", len(gps))
        for u in gps[:10]:
            print(" G", u[:160])

        # Search for JSON blobs with photo media keys
        for pat in (
            r'"photoUri"\s*:\s*"([^"]+)"',
            r'"url"\s*:\s*"(https://lh[^"]+)"',
            r'AF1Qip[A-Za-z0-9_\-]+',
            r'"mediaKey"\s*:\s*"([^"]+)"',
            r'"image"\s*:\s*"(https://lh[^"]+)"',
        ):
            hits = re.findall(pat, text)
            print(pat[:40], "->", len(hits))
            for h in hits[:5]:
                print("  ", str(h)[:140])

        # Save classified lists
        (OUT / "urls.json").write_text(
            json.dumps({k: classify(v) for k, v in uniq.items()}, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
