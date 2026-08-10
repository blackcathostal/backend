"""Try Tripadvisor traveler media album endpoints for d18941046."""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

OUT = Path(r"c:\Users\jesus\Desktop\proyecto_blackcat\backend\tmp_ta_media")
OUT.mkdir(parents=True, exist_ok=True)

LOCATION = "18941046"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    "Referer": (
        "https://www.tripadvisor.cl/Hotel_Review-g294305-d18941046-Reviews-"
        "Hostal_Boutique_Black_Cat-Santiago_Santiago_Metropolitan_Region.html"
    ),
    "Origin": "https://www.tripadvisor.cl",
    "X-Requested-With": "XMLHttpRequest",
}

URLS = [
    # Classic media gallery endpoints (historical)
    f"https://www.tripadvisor.cl/Hotel_Review-g294305-d{LOCATION}-Reviews-or0-Hostal_Boutique_Black_Cat-Santiago_Santiago_Metropolitan_Region.html",
    f"https://www.tripadvisor.com/LocationPhotoAlbum?detail={LOCATION}&albumViewMode=hero&filter=1&ajax=1",
    f"https://www.tripadvisor.cl/LocationPhotoAlbum?detail={LOCATION}&albumViewMode=hero&filter=1&ajax=1",
    f"https://www.tripadvisor.com/LocationPhotoAlbum?detail={LOCATION}&albumid=107&filter=1&ajax=1",
    f"https://www.tripadvisor.cl/LocationPhotoAlbum?detail={LOCATION}&albumid=107&filter=1&ajax=1",
    f"https://www.tripadvisor.com/data/graphql/ids",
    # Internal media
    f"https://www.tripadvisor.com/Hotel_Review-g294305-d{LOCATION}-m.html",
    f"https://www.tripadvisor.cl/Hotel_Review-g294305-d{LOCATION}-m.html",
]

PHOTO_RE = re.compile(
    r"https://(?:dynamic-)?media-cdn\.tripadvisor\.com/media/photo-[ost]/[^\s\"'<>\\]+",
    re.I,
)


def main() -> None:
    photos: set[str] = set()
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=40.0) as client:
        for url in URLS:
            try:
                if url.endswith("/ids"):
                    # try a media-related graphql payload later
                    continue
                r = client.get(url)
                text = r.text
                found = set(PHOTO_RE.findall(text))
                print(f"{r.status_code} {len(text):7d} photos={len(found):3d} {url[:100]}")
                (OUT / f"resp_{len(list(OUT.glob('resp_*'))):02d}.txt").write_text(
                    text[:20000], encoding="utf-8", errors="ignore"
                )
                photos |= {p.split("?")[0] for p in found}
            except Exception as e:
                print("FAIL", type(e).__name__, e)

        # GraphQL attempts commonly used by TA frontends
        gql_endpoints = [
            "https://www.tripadvisor.cl/data/graphql/ids",
            "https://www.tripadvisor.com/data/graphql/ids",
            "https://www.tripadvisor.cl/data/graphql",
            "https://www.tripadvisor.com/data/graphql",
        ]
        payloads = [
            [
                {
                    "variables": {
                        "locationId": int(LOCATION),
                        "albumId": 107,
                        "offset": 0,
                        "limit": 50,
                    },
                    "extensions": {"preRegisteredQueryId": "locationMedia"},
                }
            ],
            {
                "query": (
                    "query($locationId: Int!, $offset: Int!, $limit: Int!) {"
                    " locationMedia(locationId: $locationId, offset: $offset, limit: $limit,"
                    " mediaType: TRAVELER) { media { id photoSizes { url width height } } totalCount } }"
                ),
                "variables": {"locationId": int(LOCATION), "offset": 0, "limit": 50},
            },
        ]
        for endpoint in gql_endpoints:
            for payload in payloads:
                try:
                    r = client.post(
                        endpoint,
                        json=payload,
                        headers={**HEADERS, "Content-Type": "application/json"},
                    )
                    text = r.text
                    found = set(PHOTO_RE.findall(text))
                    print(
                        f"GQL {r.status_code} {len(text):7d} photos={len(found):3d} {endpoint}"
                    )
                    if r.status_code < 500 and len(text) > 20:
                        (OUT / f"gql_{abs(hash(endpoint+str(payload)))%10000}.json").write_text(
                            text[:50000], encoding="utf-8", errors="ignore"
                        )
                    photos |= {p.split("?")[0] for p in found}
                except Exception as e:
                    print("GQL FAIL", e)

    print("TOTAL UNIQUE", len(photos))
    for p in sorted(photos)[:40]:
        print(p)
    (OUT / "urls.json").write_text(json.dumps(sorted(photos), indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
