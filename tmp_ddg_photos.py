"""DuckDuckGo image search for Tripadvisor CDN photos of Black Cat Hostal."""
from __future__ import annotations

import json
import re
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image

OUT = Path(r"c:\Users\jesus\Desktop\proyecto_blackcat\frontend\public\cappa\img\viajeros-ta")
OUT.mkdir(parents=True, exist_ok=True)

QUERIES = [
    "Hostal Boutique Black Cat Santiago tripadvisor",
    "Hostal Boutique Black Cat Santiago site:tripadvisor.com",
    '"Hostal Boutique Black Cat" traveler photo',
]

PHOTO_RE = re.compile(
    r"https://(?:dynamic-)?media-cdn\.tripadvisor\.com/media/photo-[ost]/[0-9a-f/]+/[^\s\"'<>\\]+\.jpe?g",
    re.I,
)


def main() -> None:
    photos: set[str] = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    }
    with httpx.Client(headers=headers, follow_redirects=True, timeout=40.0) as client:
        for q in QUERIES:
            # DDG HTML
            url = f"https://duckduckgo.com/?q={quote(q)}&iax=images&ia=images"
            r = client.get(url)
            print("ddg html", r.status_code, len(r.text), q[:40])
            photos |= {m.split("?")[0] for m in PHOTO_RE.findall(r.text)}

            # DDG i.js token flow (simplified: vqd from page)
            vqd = None
            m = re.search(r"vqd=([\"'])(.+?)\1", r.text)
            if m:
                vqd = m.group(2)
            if not vqd:
                m = re.search(r"vqd:\s*[\"']([^\"']+)[\"']", r.text)
                if m:
                    vqd = m.group(1)
            print("vqd", bool(vqd))
            if vqd:
                api = (
                    "https://duckduckgo.com/i.js?"
                    f"l=wt-wt&o=json&q={quote(q)}&vqd={quote(vqd)}&f=,,,,,&p=1"
                )
                try:
                    ir = client.get(api, headers={**headers, "Referer": "https://duckduckgo.com/"})
                    print("i.js", ir.status_code, len(ir.text))
                    photos |= {m.split("?")[0] for m in PHOTO_RE.findall(ir.text)}
                    try:
                        data = ir.json()
                        for item in data.get("results") or []:
                            for key in ("image", "thumbnail", "url"):
                                val = item.get(key) or ""
                                if "tripadvisor.com" in val and "/media/photo-" in val:
                                    photos.add(val.split("?")[0])
                    except Exception:
                        pass
                except Exception as e:
                    print("i.js fail", e)

            # Bing again
            bing = (
                "https://www.bing.com/images/async?"
                f"q={quote(q)}&first=0&count=50&qft=+filterui:imagesize-large"
            )
            br = client.get(bing)
            photos |= {m.split("?")[0] for m in PHOTO_RE.findall(br.text)}
            for m in re.findall(r"murl&quot;:&quot;(https?://[^&]+)", br.text):
                if "tripadvisor" in m and "/media/photo-" in m:
                    photos.add(m.split("?")[0])
            print("bing", br.status_code, len(photos))

        # Always keep known listing photo
        photos.add(
            "https://dynamic-media-cdn.tripadvisor.com/media/photo-o/19/ba/5d/24/hostal-boutique-black.jpg"
        )

        print("FOUND", len(photos))
        for p in sorted(photos):
            print(p)

        saved_paths = []
        for i, url in enumerate(sorted(photos), 1):
            try:
                r = client.get(url + ("?w=1000&h=1000&s=1" if "?" not in url else ""))
                if r.status_code != 200 or len(r.content) < 4000:
                    continue
                im = Image.open(BytesIO(r.content)).convert("RGB")
                w, h = im.size
                if max(w, h) > 1200:
                    s = 1200 / max(w, h)
                    im = im.resize((int(w * s), int(h * s)), Image.Resampling.LANCZOS)
                dest = OUT / f"{i:02d}.webp"
                im.save(dest, "WEBP", quality=84, method=6)
                saved_paths.append(f"img/viajeros-ta/{dest.name}")
                print("saved", dest.name, im.size)
            except Exception as e:
                print("dl fail", e, url[-60:])

        (OUT / "local.json").write_text(json.dumps(saved_paths, indent=2), encoding="utf-8")
        print("SAVED", len(saved_paths))


if __name__ == "__main__":
    main()
