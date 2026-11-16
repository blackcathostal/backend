from __future__ import annotations

import re
from pathlib import Path

html = Path("uploads/cache/facturacion_plano_after_login.html").read_text(
    encoding="utf-8", errors="ignore"
)

print("=== refresca_formulario_Menu calls ===")
for match in re.finditer(r"refresca_formulario_Menu\(([^)]*)\)", html):
    start = max(0, match.start() - 180)
    end = min(len(html), match.end() + 100)
    ctx = " ".join(html[start:end].split())
    if any(k in ctx.lower() for k in ("compra", "venta", "libro", "excel", "export")):
        print(match.group(1))
        print(" ", ctx[:320])
        print("---")

print("=== near Libro Compras ===")
for match in re.finditer(r".{0,120}Libro Compras.{0,120}", html, re.I | re.S):
    print(" ".join(match.group(0).split())[:400])

print("=== scripts ===")
print(re.findall(r"src=['\"]([^'\"]+)['\"]", html, re.I)[:40])

print("=== forms count", len(re.findall(r"<form", html, re.I)))
for form in re.findall(r"<form[^>]*>.*?</form>", html, re.I | re.S)[:8]:
    print("FORM", " ".join(form.split())[:450])
    print("---")
