"""Fetch Tripadvisor traveler photos via free proxies + jina."""
from __future__ import annotations

import re
from pathlib import Path

import httpx

OUT = Path(r"c:\Users\jesus\Desktop\proyecto_blackcat\backend\tmp_ta_media")
OUT.mkdir(parents=True, exist_ok=True)

TARGET = (
    "https://www.tripadvisor.cl/Hotel_Review-g294305-d18941046-Reviews-"
    "Hostal_Boutique_Black_Cat-Santiago_Santiago_Metropolitan_Region.html"
)
PHOTO_RE = re.compile(
    r"https://(?:dynamic-)?media-cdn\.tripadvisor\.com/media/photo-[ost]/[^\s\"'<>\\]+",
    re.I,
)

FETCHERS = [
    f"https://r.jina.ai/http://{TARGET.replace('https://', '')}",
    f"https://r.jina.ai/{TARGET}",
    f"https://api.allorigins.win/get?url={TARGET}",
    f"https://api.codetabs.com/v1/proxy?quest={TARGET}",
]


def main() -> None:
    photos: set[str] = set()
    with httpx.Client(
        headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
        follow_redirects=True,
        timeout=60.0,
    ) as client:
        for url in FETCHERS:
            try:
                r = client.get(url)
                text = r.text
                found = {m.split("?")[0] for m in PHOTO_RE.findall(text)}
                print(r.status_code, len(text), len(found), url[:90])
                photos |= found
                if found:
                    (OUT / "hit.txt").write_text(text[:50000], encoding="utf-8", errors="ignore")
            except Exception as e:
                print("FAIL", type(e).__name__, e)

        # Try a couple free public proxies (best effort)
        proxy_list_url = "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all"
        try:
            proxies_raw = client.get(proxy_list_url, timeout=20).text.strip().splitlines()
            proxies = [p.strip() for p in proxies_raw if p.strip()][:15]
            print("proxies", len(proxies))
        except Exception as e:
            proxies = []
            print("proxy list fail", e)

        for proxy in proxies:
            try:
                with httpx.Client(
                    proxy=f"http://{proxy}",
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0",
                        "Accept-Language": "es-CL,es;q=0.9",
                    },
                    follow_redirects=True,
                    timeout=20.0,
                ) as pc:
                    r = pc.get(TARGET)
                    found = {m.split("?")[0] for m in PHOTO_RE.findall(r.text)}
                    print("via", proxy, r.status_code, len(r.text), len(found))
                    photos |= found
                    if len(found) >= 3:
                        break
            except Exception as e:
                print("proxy fail", proxy, type(e).__name__)

    print("TOTAL", len(photos))
    for p in sorted(photos):
        print(p)
    (OUT / "proxy_urls.txt").write_text("\n".join(sorted(photos)), encoding="utf-8")


if __name__ == "__main__":
    main()
