from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urljoin

import httpx

BASES = [
    "https://adminprees1.fnsmanager.com/api-doc/channel/",
    "https://adminprees1.fnsmanager.com/api-doc/pms/",
]


def main() -> None:
    with httpx.Client(timeout=45.0, follow_redirects=True) as client:
        for base in BASES:
            print("====", base)
            html = client.get(base).text
            Path("uploads/cache").mkdir(parents=True, exist_ok=True)
            name = "fns_channel_doc.html" if "channel" in base else "fns_pms_doc.html"
            Path("uploads/cache", name).write_text(html, encoding="utf-8")
            refs = set()
            for pat in [
                r"""url\s*:\s*["']([^"']+)["']""",
                r"""spec(?:Url)?\s*[:=]\s*["']([^"']+)["']""",
                r"""["']([^"']+\.json)["']""",
                r"""["']([^"']+api-docs[^"']*)["']""",
            ]:
                refs.update(re.findall(pat, html, flags=re.I))
            print("refs", sorted(refs)[:30])
            for ref in sorted(refs):
                if ref.startswith("http") or ref.endswith(".json") or "api-docs" in ref or "swagger" in ref.lower():
                    url = ref if ref.startswith("http") else urljoin(base, ref)
                    try:
                        r = client.get(url)
                    except Exception as exc:  # noqa: BLE001
                        print(" ERR", url, exc)
                        continue
                    print(" ", r.status_code, url, r.text[:100].replace("\n", " "))
                    if r.status_code < 400 and r.text.strip().startswith("{"):
                        data = r.json()
                        paths = list((data.get("paths") or {}).keys())
                        print("  PATHS", len(paths))
                        for p in paths[:60]:
                            methods = ",".join(sorted((data["paths"][p] or {}).keys()))
                            print(f"   {methods:10} {p}")
                        sec = data.get("securityDefinitions") or data.get("components")
                        print("  security", str(sec)[:300])


if __name__ == "__main__":
    main()
