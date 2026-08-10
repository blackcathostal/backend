"""
Interactive Tripadvisor traveler-photo sync.
Opens a visible browser so you can pass the captcha, then downloads album photos.
"""
from __future__ import annotations

import json
import re
import time
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image
from playwright.sync_api import sync_playwright

OUT = Path(r"c:\Users\jesus\Desktop\proyecto_blackcat\frontend\public\cappa\img\viajeros-ta")
OUT.mkdir(parents=True, exist_ok=True)
CACHE = Path(r"c:\Users\jesus\Desktop\proyecto_blackcat\backend\uploads\cache")
CACHE.mkdir(parents=True, exist_ok=True)

LISTING = (
    "https://www.tripadvisor.cl/Hotel_Review-g294305-d18941046-Reviews-"
    "Hostal_Boutique_Black_Cat-Santiago_Santiago_Metropolitan_Region.html"
)
ALBUM = LISTING + "#/media/18941046/?type=TRAVELER&albumid=107&category=107"

PHOTO_RE = re.compile(
    r"https://(?:dynamic-)?media-cdn\.tripadvisor\.com/media/photo-[ost]/[^\s\"'<>\\]+",
    re.I,
)


def normalize(url: str) -> str:
    url = url.replace("\\u002F", "/").replace("\\/", "/").split("?")[0]
    url = url.replace("https://media-cdn.tripadvisor.com", "https://dynamic-media-cdn.tripadvisor.com")
    if "media-cdn.tripadvisor.com" not in url:
        return ""
    if any(x in url.lower() for x in ("logo", "avatar", "icon", "sprite", "badge")):
        return ""
    return url


def main() -> None:
    photos: set[str] = set()
    print("Abriendo Tripadvisor. Si ves captcha, resuelvelo en la ventana.")
    print("Cuando cargue el album de fotos de viajeros, el script las captura.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=50)
        context = browser.new_context(
            locale="es-CL",
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        def on_response(resp):
            try:
                u = resp.url
                if "media-cdn.tripadvisor.com" in u and "/media/photo-" in u:
                    n = normalize(u)
                    if n:
                        photos.add(n)
                ct = (resp.headers.get("content-type") or "").lower()
                if "json" in ct or "javascript" in ct or "text" in ct:
                    if any(k in u.lower() for k in ("photo", "media", "album", "graphql")):
                        try:
                            body = resp.text()
                            for m in PHOTO_RE.findall(body):
                                n = normalize(m)
                                if n:
                                    photos.add(n)
                        except Exception:
                            pass
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(LISTING, wait_until="domcontentloaded", timeout=180000)

        # Wait up to ~3 minutes for user to pass captcha / open album
        deadline = time.time() + 180
        while time.time() < deadline:
            title = page.title()
            html = page.content()
            blocked = "restringido" in html.lower() or "captcha" in html.lower()
            for m in PHOTO_RE.findall(html):
                n = normalize(m)
                if n:
                    photos.add(n)
            print(f"photos={len(photos)} title={title[:50]!r} blocked={blocked}")
            if len(photos) >= 8 and not blocked:
                break
            # Try navigate to album hash once unblocked
            if not blocked and "media/" not in page.url:
                try:
                    page.goto(ALBUM, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
            try:
                page.mouse.wheel(0, 1600)
            except Exception:
                pass
            time.sleep(5)

        # Final harvest
        for m in PHOTO_RE.findall(page.content()):
            n = normalize(m)
            if n:
                photos.add(n)
        srcs = page.eval_on_selector_all(
            "img", "els => els.map(e => e.currentSrc || e.src || '')"
        )
        for s in srcs:
            n = normalize(s or "")
            if n:
                photos.add(n)

        browser.close()

    urls = sorted(photos)
    print("UNIQUE", len(urls))
    for u in urls:
        print(u)

    saved = []
    entries = []
    with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True, timeout=40) as client:
        for i, url in enumerate(urls, 1):
            try:
                full = url if "?" in url else url + "?w=1200&h=1200&s=1"
                r = client.get(full)
                if r.status_code != 200 or len(r.content) < 4000:
                    continue
                im = Image.open(BytesIO(r.content)).convert("RGB")
                w, h = im.size
                if max(w, h) > 1400:
                    s = 1400 / max(w, h)
                    im = im.resize((int(w * s), int(h * s)), Image.Resampling.LANCZOS)
                name = f"{i:02d}.webp"
                dest = OUT / name
                im.save(dest, "WEBP", quality=85, method=6)
                rel = f"img/viajeros-ta/{name}"
                saved.append(rel)
                entries.append({"id": str(i), "url": full, "local": rel, "source": "Traveler"})
                print("saved", name, im.size)
            except Exception as e:
                print("fail", e)

    payload = {
        "location_id": "18941046",
        "total": max(70, len(entries)),
        "count": len(entries),
        "photos": entries,
        "album_url": ALBUM,
        "listing_url": LISTING,
        "source": "tripadvisor_traveler_album",
        "synced_at": int(time.time()),
        "live": True,
        "provider": "tripadvisor",
    }
    (CACHE / "tripadvisor_photos.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "local.json").write_text(json.dumps(saved, indent=2), encoding="utf-8")
    print("DONE", len(saved))


if __name__ == "__main__":
    main()
