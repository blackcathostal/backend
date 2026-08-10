"""Playwright with persistent profile + slow browsing for TA traveler album."""
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
META = OUT / "manifest.json"
PROFILE = Path(r"c:\Users\jesus\Desktop\proyecto_blackcat\backend\tmp_pw_profile")
PROFILE.mkdir(parents=True, exist_ok=True)

URL = (
    "https://www.tripadvisor.cl/Hotel_Review-g294305-d18941046-Reviews-"
    "Hostal_Boutique_Black_Cat-Santiago_Santiago_Metropolitan_Region.html"
    "#/media/18941046/?type=TRAVELER&albumid=107&category=107"
)

PHOTO_RE = re.compile(
    r"https://(?:dynamic-)?media-cdn\.tripadvisor\.com/media/photo-[ost]/[^\s\"'<>\\]+",
    re.I,
)


def normalize(url: str) -> str:
    url = url.replace("\\u002F", "/").replace("\\/", "/").split("?")[0]
    url = url.replace("https://media-cdn.tripadvisor.com", "https://dynamic-media-cdn.tripadvisor.com")
    if "media-cdn.tripadvisor.com" not in url:
        return ""
    low = url.lower()
    if any(x in low for x in ("logo", "avatar", "icon", "sprite", "badge", "lazy")):
        return ""
    return url + "?w=1000&h=1000&s=1"


def main() -> None:
    photos: set[str] = set()
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=True,
            locale="es-CL",
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
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
                if any(x in ct for x in ("json", "javascript", "text")) and (
                    "photo" in u.lower() or "media" in u.lower() or "album" in u.lower() or "graphql" in u.lower()
                ):
                    try:
                        body = resp.text()
                        for m in PHOTO_RE.findall(body):
                            n = normalize(m)
                            if n:
                                photos.add(n)
                        # also save interesting json
                        if "graphql" in u.lower() or "photo" in u.lower():
                            safe = re.sub(r"[^\w.-]+", "_", u)[-80:]
                            Path(r"c:\Users\jesus\Desktop\proyecto_blackcat\backend\tmp_ta_media").mkdir(
                                exist_ok=True
                            )
                            Path(
                                rf"c:\Users\jesus\Desktop\proyecto_blackcat\backend\tmp_ta_media\{safe}.txt"
                            ).write_text(body[:100000], encoding="utf-8", errors="ignore")
                    except Exception:
                        pass
            except Exception:
                pass

        page.on("response", on_response)

        print("GOTO hotel page first")
        page.goto(URL.split("#")[0], wait_until="domcontentloaded", timeout=120000)
        time.sleep(8)
        print("title", page.title())
        page.screenshot(
            path=r"c:\Users\jesus\Desktop\proyecto_blackcat\backend\tmp_ta_media\shot1.png"
        )

        # Try open media hash URL
        print("GOTO media album hash")
        page.goto(URL, wait_until="domcontentloaded", timeout=120000)
        time.sleep(10)
        print("title2", page.title(), "photos", len(photos))
        page.screenshot(
            path=r"c:\Users\jesus\Desktop\proyecto_blackcat\backend\tmp_ta_media\shot2.png"
        )

        # Click traveler photos / fotos
        for sel in (
            "a:has-text('Fotos')",
            "button:has-text('Fotos')",
            "a:has-text('Photos')",
            "button:has-text('Viajero')",
            "button:has-text('Traveler')",
            "[data-automation='photoAlbum']",
            "a[href*='albumid=107']",
            "a[href*='type=TRAVELER']",
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1500):
                    loc.click()
                    time.sleep(4)
                    print("clicked", sel, "photos", len(photos))
            except Exception:
                pass

        for _ in range(20):
            page.mouse.wheel(0, 2000)
            time.sleep(0.6)
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

        print("FINAL photos", len(photos))
        page.screenshot(
            path=r"c:\Users\jesus\Desktop\proyecto_blackcat\backend\tmp_ta_media\shot3.png",
            full_page=False,
        )
        context.close()

    urls = sorted(photos)
    META.write_text(
        json.dumps({"count": len(urls), "urls": urls, "album": URL}, indent=2),
        encoding="utf-8",
    )
    print("URLS", len(urls))
    for u in urls:
        print(u)

    saved = []
    with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True, timeout=40) as client:
        for i, url in enumerate(urls, 1):
            try:
                r = client.get(url)
                if r.status_code != 200 or len(r.content) < 4000:
                    print("skip", r.status_code, len(r.content))
                    continue
                im = Image.open(BytesIO(r.content)).convert("RGB")
                w, h = im.size
                if max(w, h) > 1200:
                    s = 1200 / max(w, h)
                    im = im.resize((int(w * s), int(h * s)), Image.Resampling.LANCZOS)
                dest = OUT / f"{i:02d}.webp"
                im.save(dest, "WEBP", quality=84, method=6)
                saved.append(f"img/viajeros-ta/{dest.name}")
                print("saved", dest.name, im.size)
            except Exception as e:
                print("dl", e)
    print("SAVED", len(saved))
    (OUT / "local.json").write_text(json.dumps(saved, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
