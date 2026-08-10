"""Download ONLY Black Cat Hostal photos from Google Travel photo viewer."""
from __future__ import annotations

import json
import re
import time
from io import BytesIO
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

OUT = Path(r"c:\Users\jesus\Desktop\proyecto_blackcat\frontend\public\cappa\img\viajeros-google")
CACHE = Path(r"c:\Users\jesus\Desktop\proyecto_blackcat\backend\uploads\cache")
OUT.mkdir(parents=True, exist_ok=True)
CACHE.mkdir(parents=True, exist_ok=True)

# Direct hotel entity + photos mode
URLS = [
    (
        "https://www.google.com/travel/hotels/entity/CgoI-_8b-DDKwptAEAE"
        "?g2lb=4965990&hl=es-419&gl=cl&ap=MAC6AQZwaG90b3M&q=black%20cat%20hostal"
    ),
    (
        "https://www.google.com/maps/place/data=!4m6!3m5!1s0x9662c5b8477cf75b:0x9bc2ca30f81b6eff"
        "!8m2!3d-33.4408!4d-70.6665!16s%2Fg%2F11j0_example?hl=es-419&entry=ttu"
    ),
    (
        "https://www.google.com/maps/place/Black+Cat+Hostal+Boutique/"
        "@-33.440879,-70.6665,17z/data=!4m8!3m7!1s0x9662c5b8477cf75b:0x9bc2ca30f81b6eff"
        "!5m2!4m1!1i2!8m2!3d-33.440879!4d-70.6665?hl=es-419&entry=ttu"
    ),
]

ALBUM_URL = (
    "https://www.google.com/travel/search?q=black%20cat%20hostal&hl=es-419&gl=cl"
    "&ap=MAC6AQZwaG90b3M&ictx=111"
    "&ts=CAEaSQopEicyJTB4OTY2MmM1Yjg0NzdjZjc1YjoweDliYzJjYTMwZjgxYjZlZmYSHBIUCgcI6g8QBxgaEgcI6g8QBxgbGAEyBAgAEAAqBwoFOgNDTFA"
    "&qs=CAEyFENnc0lfOTN0d0lfR3N1R2JBUkFCOAJCCQn_bhv4MMrCm0IJCf9uG_gwysKbSAA"
)


def main() -> None:
    captured: list[bytes] = []
    seen: set[int] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="es-419",
            viewport={"width": 1500, "height": 960},
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
                # Skip tiny thumbs and avatars
                if any(x in u for x in ("/a-/", "=s32", "=s64", "=w32", "=w64", "rp-mo")):
                    return
                ct = (resp.headers.get("content-type") or "").lower()
                if "image" not in ct:
                    return
                # Prefer hotel media (gps-cs-s / p / proxy large)
                if not any(x in u for x in ("/gps-cs-s/", "/p/", "/proxy/")):
                    return
                body = resp.body()
                if not body or len(body) < 12000:
                    return
                h = hash(body[:8000])
                if h in seen:
                    return
                # Reject very small decoded images later
                seen.add(h)
                captured.append(body)
            except Exception:
                pass

        page.on("response", on_response)

        # 1) Maps place page photos
        maps = (
            "https://www.google.com/maps/place/Black+Cat+Hostal+Boutique/"
            "@-33.440879,-70.6665,17z/data=!3m1!4b1!4m6!3m5!"
            "1s0x9662c5b8477cf75b:0x9bc2ca30f81b6eff!8m2!3d-33.440879!4d-70.6665"
            "!16s%2Fg%2F11j8xqkexample?entry=ttu&hl=es-419"
        )
        print("GOTO maps")
        page.goto(maps, wait_until="domcontentloaded", timeout=120000)
        time.sleep(6)
        print("title", page.title())

        for sel in (
            "button:has-text('Fotos')",
            "button:has-text('Photos')",
            "a:has-text('Fotos')",
            "a:has-text('Photos')",
            "[aria-label*='Foto']",
            "[aria-label*='Photo']",
            "button[jsaction*='pane.hero']",
            "button[aria-label*='fotos']",
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=2000):
                    loc.click()
                    print("clicked", sel)
                    time.sleep(3)
                    break
            except Exception:
                pass

        # Click first photo tile to open lightbox
        for sel in (
            "button[aria-label*='Foto 1']",
            "button[aria-label*='Photo 1']",
            "[role='img']",
            "img[src*='googleusercontent']",
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1500):
                    loc.click()
                    print("open lightbox", sel)
                    time.sleep(2)
                    break
            except Exception:
                pass

        # Navigate through gallery with arrow keys / next
        for i in range(40):
            try:
                page.keyboard.press("ArrowRight")
            except Exception:
                pass
            time.sleep(0.7)
            if i % 8 == 0:
                print("gallery step", i, "captured", len(captured))

        # Also try Travel entity photos page
        print("GOTO travel entity")
        page.goto(URLS[0], wait_until="domcontentloaded", timeout=120000)
        time.sleep(6)
        print("title2", page.title())
        for sel in (
            "text=Todas las fotos",
            "text=All photos",
            "text=Fotos",
            "button:has-text('Viajero')",
            "button:has-text('Traveler')",
            "button:has-text('Huésped')",
            "button:has-text('Guest')",
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1500):
                    loc.click()
                    time.sleep(2)
                    print("travel click", sel)
            except Exception:
                pass

        for _ in range(20):
            page.mouse.wheel(0, 1600)
            time.sleep(0.45)

        # Click several visible hotel grid images
        imgs = page.locator("img[src*='googleusercontent']")
        count = min(imgs.count(), 30)
        for i in range(count):
            try:
                imgs.nth(i).click(timeout=1000)
                time.sleep(0.8)
                page.keyboard.press("Escape")
                time.sleep(0.3)
            except Exception:
                pass

        page.screenshot(path=str(Path(r"c:\Users\jesus\Desktop\proyecto_blackcat\backend\tmp_gtravel\hotel_shot.png")))
        browser.close()

    print("RAW captured", len(captured))

    # Clear and rebuild collage folder
    for old in OUT.glob("*"):
        if old.is_file():
            old.unlink()

    saved = []
    for body in captured:
        try:
            im = Image.open(BytesIO(body)).convert("RGB")
            w, h = im.size
            # Keep only reasonably large photos (not nearby thumbs)
            if min(w, h) < 250 or max(w, h) < 500:
                continue
            if max(w, h) > 1400:
                s = 1400 / max(w, h)
                im = im.resize((int(w * s), int(h * s)), Image.Resampling.LANCZOS)
            name = f"{len(saved)+1:02d}.webp"
            dest = OUT / name
            im.save(dest, "WEBP", quality=85, method=6)
            saved.append(f"img/viajeros-google/{name}")
            print("saved", name, im.size)
            if len(saved) >= 48:
                break
        except Exception as e:
            print("fail", e)

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
        "album_url": ALBUM_URL,
        "listing_url": ALBUM_URL,
        "source": "google_travel_hotel_only",
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
