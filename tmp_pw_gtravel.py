"""Use Playwright to open Google Travel photos and download visitor images."""
from __future__ import annotations

import json
import re
import time
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image
from playwright.sync_api import sync_playwright

OUT = Path(r"c:\Users\jesus\Desktop\proyecto_blackcat\frontend\public\cappa\img\viajeros-google")
CACHE = Path(r"c:\Users\jesus\Desktop\proyecto_blackcat\backend\uploads\cache")
OUT.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(parents=True, exist_ok=True)

URL = (
    "https://www.google.com/travel/search?q=black%20cat%20hostal"
    "&hl=es-419&gl=cl&ssta=1"
    "&ts=CAEaSQopEicyJTB4OTY2MmM1Yjg0NzdjZjc1YjoweDliYzJjYTMwZjgxYjZlZmYSHBIUCgcI6g8QBxgaEgcI6g8QBxgbGAEyBAgAEAAqBwoFOgNDTFA"
    "&qs=CAEyFENnc0lfOTN0d0lfR3N1R2JBUkFCOAJCCQn_bhv4MMrCm0IJCf9uG_gwysKbSAA"
    "&ap=MAC6AQZwaG90b3M&ictx=111"
)

PHOTO_RE = re.compile(r"https://lh[0-9]\.googleusercontent\.com/[^\s\"'<>\\]+", re.I)


def normalize(url: str) -> str:
    url = url.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
    url = url.split("?")[0].split('"')[0].split("'")[0]
    if any(x in url.lower() for x in ("/a-/", "rp-mo", "-mo-ba", "logo", "icon")):
        return ""
    # Prefer large
    if "/gps-cs-s/" in url or "/p/" in url or "/proxy/" in url:
        if "=" in url and url.rstrip("=").split("/")[-1].startswith(("AH", "AF", "AHR")):
            return url.split("=")[0] + "=w1600-h1600-k-no"
        if url.count("=") == 0:
            return url + "=w1600-h1600-k-no"
        return re.sub(r"=.*$", "=w1600-h1600-k-no", url)
    return ""


def main() -> None:
    photos: set[str] = set()
    binary_hits: list[tuple[str, bytes]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="es-419",
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        def on_response(resp):
            try:
                u = resp.url
                if "googleusercontent.com" not in u:
                    return
                n = normalize(u)
                if n:
                    photos.add(n.split("=")[0] + "=w1600-h1600-k-no")
                ct = (resp.headers.get("content-type") or "").lower()
                if "image" in ct and ("/gps-cs-s/" in u or "/p/" in u):
                    try:
                        body = resp.body()
                        if body and len(body) > 8000:
                            binary_hits.append((u, body))
                    except Exception:
                        pass
            except Exception:
                pass

        page.on("response", on_response)
        print("GOTO", URL[:80])
        page.goto(URL, wait_until="domcontentloaded", timeout=120000)
        time.sleep(5)
        print("title", page.title())

        # Click photos / fotos tabs if present
        for sel in (
            "text=Fotos",
            "text=Photos",
            "[aria-label*='Foto']",
            "[aria-label*='Photo']",
            "button:has-text('Todas')",
            "button:has-text('Viajeros')",
            "button:has-text('Huéspedes')",
            "button:has-text('Traveler')",
            "button:has-text('Guest')",
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1500):
                    loc.click()
                    time.sleep(2)
                    print("clicked", sel)
            except Exception:
                pass

        for _ in range(25):
            page.mouse.wheel(0, 1800)
            time.sleep(0.5)

        html = page.content()
        for m in PHOTO_RE.findall(html):
            n = normalize(m)
            if n:
                photos.add(n)

        srcs = page.eval_on_selector_all(
            "img",
            "els => els.map(e => e.currentSrc || e.src || '')",
        )
        for s in srcs:
            n = normalize(s or "")
            if n:
                photos.add(n)

        page.screenshot(path=str(CACHE.parent / "gtravel_shot.png"))
        browser.close()

    print("URL photos", len(photos), "binary", len(binary_hits))

    # Reset output folder (only webp collage assets)
    for old in OUT.glob("*.webp"):
        old.unlink()
    for old in OUT.glob("local.json"):
        old.unlink()

    saved = []
    # Prefer images captured from network responses (already decoded)
    seen_hash: set[int] = set()
    for i, (url, body) in enumerate(binary_hits, 1):
        try:
            h = hash(body[:5000])
            if h in seen_hash:
                continue
            seen_hash.add(h)
            im = Image.open(BytesIO(body)).convert("RGB")
            w, hgt = im.size
            if min(w, hgt) < 180:
                continue
            if max(w, hgt) > 1400:
                s = 1400 / max(w, hgt)
                im = im.resize((int(w * s), int(hgt * s)), Image.Resampling.LANCZOS)
            name = f"{len(saved)+1:02d}.webp"
            dest = OUT / name
            im.save(dest, "WEBP", quality=85, method=6)
            saved.append(f"img/viajeros-google/{name}")
            print("bin", name, im.size, url[-50:])
        except Exception as e:
            print("bin fail", e)

    # Fallback download remaining URLs with browser-like referer
    with httpx.Client(
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.google.com/",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
        follow_redirects=True,
        timeout=40.0,
    ) as client:
        for url in sorted(photos):
            if len(saved) >= 48:
                break
            try:
                r = client.get(url)
                if r.status_code != 200 or len(r.content) < 8000:
                    continue
                h = hash(r.content[:5000])
                if h in seen_hash:
                    continue
                seen_hash.add(h)
                im = Image.open(BytesIO(r.content)).convert("RGB")
                w, hgt = im.size
                if min(w, hgt) < 180:
                    continue
                if max(w, hgt) > 1400:
                    s = 1400 / max(w, hgt)
                    im = im.resize((int(w * s), int(hgt * s)), Image.Resampling.LANCZOS)
                name = f"{len(saved)+1:02d}.webp"
                dest = OUT / name
                im.save(dest, "WEBP", quality=85, method=6)
                saved.append(f"img/viajeros-google/{name}")
                print("dl", name, im.size)
            except Exception as e:
                print("dl fail", e)

    entries = [
        {
            "id": Path(p).stem,
            "url": f"/cappa/{p}",
            "local": p,
            "source": "Google",
        }
        for p in saved
    ]
    (OUT / "local.json").write_text(json.dumps(saved, indent=2), encoding="utf-8")
    payload = {
        "total": len(entries),
        "count": len(entries),
        "photos": entries,
        "album_url": URL,
        "listing_url": URL,
        "source": "google_travel_playwright",
        "synced_at": int(time.time()),
        "live": True,
        "provider": "google",
    }
    (CACHE / "google_photos.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("SAVED", len(saved))


if __name__ == "__main__":
    main()
