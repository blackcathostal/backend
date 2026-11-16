from __future__ import annotations

import re
from pathlib import Path

from app.core.config import settings
from app.services.facturacion_web import FacturacionWebClient

OUT = Path("uploads/cache")
OUT.mkdir(parents=True, exist_ok=True)


def main() -> None:
    print(
        "creds",
        settings.facturacion_web_empresa,
        settings.facturacion_web_user,
        bool(settings.facturacion_web_clave),
    )
    with FacturacionWebClient() as client:
        client.login()
        print("base", client.base_url)
        html, fields = client._load_compras_form()
        OUT.joinpath("angelica_compras_form.html").write_text(
            html, encoding="utf-8", errors="ignore"
        )
        print(
            "form len",
            len(html),
            "empty",
            "No se encontraron resultados" in html,
            "fields",
            sorted(fields.keys())[:20],
        )
        # show default selected period if any
        print("anno/mes defaults", fields.get("anno"), fields.get("mes"))

        for year, month in ((2026, 11), (2026, 10), (2026, 9), (2025, 11), (2025, 10)):
            page = client._search_month(year=year, month=month, base_fields=fields)
            OUT.joinpath(f"angelica_{year}_{month:02d}.html").write_text(
                page, encoding="utf-8", errors="ignore"
            )
            parsed = client._parse_grid_html(page)
            # count data rows roughly
            trs = re.findall(r"tr_lista_fila", page, re.I)
            print(
                year,
                month,
                "len",
                len(page),
                "empty_msg",
                "No se encontraron resultados" in page,
                "tr_lista_fila",
                len(trs),
                "parsed",
                len(parsed),
            )
            if parsed:
                print(" sample", parsed[0])
            else:
                # dump a bit of grid
                m = re.search(r'class=["\']LIBROS_GRID["\'][\s\S]{0,2500}', page, re.I)
                if m:
                    print(" grid snip", " ".join(m.group(0).split())[:500])

            excel = client._download_excel(
                year=year, month=month, fields=client._extract_fields(page) or fields
            )
            print(
                "  excel",
                None if excel is None else (len(excel), excel[:12]),
            )


if __name__ == "__main__":
    main()
