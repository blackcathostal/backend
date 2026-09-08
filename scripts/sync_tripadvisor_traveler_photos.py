"""Download Tripadvisor TRAVELER album photos into frontend/public/cappa/img/viajeros-ta."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image
from playwright.async_api import async_playwright

ALBUM = (
    "https://www.tripadvisor.cl/Hotel_Review-g294305-d18941046-Reviews-"
    "Hostal_Boutique_Black_Cat-Santiago_Santiago_Metropolitan_Region.html"
    "#/media/18941046/?albumid=107&type=TRAVELER&category=107"
)
OUT = Path(__file__).resolve().parents[1] / "frontend" / "public" / "cappa" / "img" / "viajeros-ta"
PHOTO_RE = re.compile(
    r"https://(?:dynamic-)?media-cdn\.tripadvisor\.com/media/photo-[ost]/[a-zA-Z0-9/_.\-]+",
    re.I,
)


def normalize(url: str) -> str:
    url = (url or "").strip().split("?")[0]
    url = url.replace("https://media-cdn.tripadvisor.com", "https://dynamic-media-cdn.tripadvisor.com")
    if "media-cdn.tripadvisor.com" not in url or "/media/photo-" not in url:
        return ""
    low = url.lower()
    if any(x in low for x in ("logo", "avatar", "icon", "sprite", "badge", "map")):
        return ""
    return url


async def collect_urls() -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 1000})
        await page.goto(ALBUM, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(4000)
        for _ in range(25):
            html = await page.content()
            for match in PHOTO_RE.findall(html):
                n = normalize(match)
                if n and n not in seen:
                    seen.add(n)
                    urls.append(n)
            # also from img src attributes
            srcs = await page.eval_on_selector_all(
                "img",
                "els => els.map(e => e.currentSrc || e.src).filter(Boolean)",
            )
            for src in srcs:
                n = normalize(src)
                if n and n not in seen:
                    seen.add(n)
                    urls.append(n)
            await page.mouse.wheel(0, 1800)
            await page.wait_for_timeout(700)
        await browser.close()
    return urls


async def download(urls: list[str]) -> list[str]:
    OUT.mkdir(parents=True, exist_ok=True)
    # clear previous traveler set (keep only synced traveler photos)
    for old in OUT.glob("*.webp"):
        old.unlink()
    saved_locals: list[str] = []
    hashes: set[str] = set()
    async with httpx.AsyncClient(
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.tripadvisor.cl/",
        },
        follow_redirects=True,
        timeout=40.0,
    ) as client:
        for index, url in enumerate(urls, 1):
            try:
                response = await client.get(f"{url}?w=1200&h=1200&s=1")
                if response.status_code != 200 or len(response.content) < 4000:
                    continue
                digest = hashlib.sha1(response.content).hexdigest()
                if digest in hashes:
                    continue
                hashes.add(digest)
                image = Image.open(BytesIO(response.content)).convert("RGB")
                w, h = image.size
                if max(w, h) > 1400:
                    scale = 1400 / max(w, h)
                    image = image.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
                name = f"{len(saved_locals)+1:02d}.webp"
                dest = OUT / name
                image.save(dest, "WEBP", quality=85, method=6)
                saved_locals.append(f"img/viajeros-ta/{name}")
                print(f"saved {name} from {url[-50:]}")
            except Exception as exc:
                print("skip", index, exc)
    (OUT / "local.json").write_text(json.dumps(saved_locals, indent=2), encoding="utf-8")
    (OUT / "manifest.json").write_text(
        json.dumps({"count": len(saved_locals), "album": ALBUM, "urls": urls[: len(saved_locals)]}, indent=2),
        encoding="utf-8",
    )
    return saved_locals


async def main() -> None:
    print("Collecting Tripadvisor traveler album URLs...")
    urls = await collect_urls()
    print(f"Found {len(urls)} unique CDN URLs")
    saved = await download(urls)
    print(f"Downloaded {len(saved)} unique traveler photos -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
