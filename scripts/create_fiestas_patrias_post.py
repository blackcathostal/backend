"""Create Fiestas Patrias 2026 fondas blog post via production API."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

API = "https://api.blackcathostal.com/api"
ADMIN_EMAIL = "admin@blackcathostal.com"
ADMIN_PASSWORD = "admin123"

TITLE = "Fiestas Patrias 2026 en Santiago: fondas, precios, artistas e itinerarios"
SLUG = "fiestas-patrias-2026-fondas-santiago-precios-artistas"

EXCERPT = (
    "Guía práctica para celebrar el 18 en Santiago: Parque O'Higgins, Ñuñoa, "
    "Semana de la Chilenidad, Renca y Maipú, con fechas, entradas y carteles."
)

BODY = """
Las **Fiestas Patrias 2026** llegan con un fin de semana largo de celebración: el **18 y 19 de septiembre** caen en viernes y sábado, y las principales fondas de la Región Metropolitana concentran shows, cocinerías y cueca entre el **17 y el 20 de septiembre**. Si te hospedas en el centro —por ejemplo en **Barrio Brasil**, a pasos de metro y buses— puedes armar un itinerario sin depender del auto.

Aquí tienes un panorama actualizado de **fondas, precios de entrada, artistas e itinerarios**, con datos publicados por municipios y medios. Los valores pueden cambiar según la etapa de venta: confirma siempre en Ticketplus, Ticketpro o el sitio oficial de cada comuna.

## Calendario rápido 2026

- **18 de septiembre (viernes):** Día de la Independencia.
- **19 de septiembre (sábado):** Día de las Glorias del Ejército.
- **Fondas grandes:** en general del **jueves 17 al domingo 20**.
- **Semana de la Chilenidad (Parque Padre Hurtado):** arranca antes, el **12 y 13**, y continúa del **16 al 20**.

## 1. La Gran Fonda de Chile – Parque O'Higgins (Santiago)

La fonda oficial de la Municipalidad de Santiago vuelve al **Parque O'Higgins**, con escenario principal, gastronomía, juegos y cuecódromo. Es la opción más “clásica” si quieres el ambiente masivo del dieciocho capitalino.

**Fechas:** 17 al 20 de septiembre de 2026 (inauguración previa anunciada para el 16).

**Horarios referenciales:**
- Jueves 17: 12:00 a 02:00
- Viernes 18 y sábado 19: 11:00 a 02:00
- Domingo 20: 11:00 a 20:00

**Entradas (Ticketplus – valores publicados en preventa / día de evento):**
- Preventa reciente: general **$13.225**; niños y +65 **$9.775**
- Preventa posterior: general **$14.950**; niños y +65 **$11.500**
- Días del evento: general **$16.000**; niños y +65 **$13.000**
- Niños hasta 12 años y adultos mayores desde 65: **gratis hasta las 14:00** todos los días (según anuncio municipal)

**Parrilla artística (escenario principal):**
- **Jueves 17:** Sinaka, Santaferia, Los Viking's 5, Bohemia Latina
- **Viernes 18:** Jairo Vera, Noche de Brujas, Luis Lambis
- **Sábado 19:** Young Cister, Zúmbale Primo, Leo Rey
- **Domingo 20 (familiar):** Cachureos, Los Sudakas, Los Jaivas

**Tip:** la entrada da acceso al recinto y a los shows del escenario; comida, bebidas y juegos se pagan aparte. Compra con anticipación: los 18 y 19 suelen ser los días más congestados.

**Cómo llegar desde el centro / Barrio Brasil:** metro o bus hacia Parque O'Higgins (estaciones cercanas como **Parque O'Higgins** o **Toesca**, según línea y combinaciones). Evita el auto si puedes: estacionar el finde dieciochero es lento y caro.

## 2. Fonda Corazón de Chile – Parque Estadio Nacional (Ñuñoa)

Ñuñoa presenta **“Corazón de Chile”** en el **Parque Estadio Nacional**: propuesta familiar de día (cocinerías, juegos, cueca, circo) y shows fuertes de noche.

**Fechas:** 17, 18, 19 y 20 de septiembre de 2026.

**Entradas (Ticketpro):**
- General desde **$12.000** (+ cargo por servicio; algunos reportes mencionan valores cerca de **$13.800** según etapa)
- Vecinos con **Tarjeta Vecino Ñuñoa:** precio preferencial (alrededor de **$10.000**)
- Beneficios adicionales para adultos mayores según anuncio municipal

**Artistas confirmados (parcial):** Santaferia, Los Vásquez, Los Prisioneros (voz de Miguel Tapia), Jere Klein, Antonio Ríos, Reina Isabel, Sigrid Alegría, Caña Brava, Sinergia (con Tía Pucherito), Pibes Chorros, Legado de Zalo, Gran Magia Tropical, entre otros. Animación anunciada con **Eva Gómez** y **José Antonio Neme**.

**Tip:** llega temprano el viernes y sábado. El **metro** es la opción más cómoda para el sector Estadio Nacional.

## 3. Semana de la Chilenidad – Parque Padre Hurtado (La Reina)

Más que una sola noche de fonda: es una **semana de chilenidad** con actividades ecuestres, folclor, gastronomía, artesanía y shows. Ideal si quieres varios días sin repetir siempre el mismo parque.

**Fechas:** 12 y 13 de septiembre, y del 16 al 20 de septiembre de 2026.

**Entradas (Ticketplus – preventas publicadas):**
- Pase diario adulto: desde **$10.000** (preventa 1) / **$12.000** (preventa 2)
- Pase 3 días: **$25.000** / **$30.000**
- Pase 5 días: **$40.000** / **$45.000**
- Niño y adulto mayor (pase diario): desde **$5.000** / **$6.000**
- Los valores no siempre incluyen cargo por servicio

**Artistas anunciados (ejemplos):** Stefan Kramer, Entremares, Los Frutantes, entre otros de la programación cultural y familiar.

**Tip:** si viajas con niños o quieres combinar tradición y paseo al aire libre, esta es una de las opciones más completas. El parque está en **Av. Francisco Bilbao**, La Reina.

## 4. Gran Fonda Familiar – Parque Las Palmeras (Renca)

Una de las alternativas **más económicas** del Gran Santiago, con foco familiar y aforo limitado por jornada.

**Fechas:** 18, 19 y 20 de septiembre de 2026.

**Entradas (somosrenca.cl):**
- **$1.000** con Tarjeta Vecino Somos Renca
- **$5.000** público general
- Niños y niñas de **0 a 13 años: gratis** (igual deben sacar su entrada/QR)

**Artistas anunciados:** Los Charros de Lumaco, Amar Azul, Sexta Cumbiera, Capibarrap, La Zaga, El Azote, Herencia Cuequera, Toque de Queda, Kllejeros, además de la Gran Mesa de Folklore de Renca.

**Tip:** reserva con tiempo: el aforo es limitado y exigen documento de identidad al ingresar.

## 5. Fiestas Patrias en la Plaza – Maipú (entrada liberada)

Si buscas celebrar **sin pagar ticket**, Maipú programa su fiesta costumbrista en la **Plaza de Maipú** con gastronomía, feria de emprendimiento, juegos y música en vivo.

**Fechas:** 17 al 20 de septiembre de 2026 · **entrada gratuita**.

**Itinerario musical anunciado:**
- **Jueves 17 (Fonda Terremoto Mayor, 12:00–18:00):** Sonora Palacios
- **Viernes 18 (12:00–22:00):** Caleuchístico, Sonora Malecón
- **Sábado 19 (12:00–22:00):** El Bloque 8, Leo Rey
- **Domingo 20 (12:00–22:00):** Mi Perro Chocolo (infantil), Sigrid y Los Claveles

## Otras opciones a tener en el radar

- **Gran Fonda Las Vizcachas** (Puente Alto / sector sur): 18 y 19 de septiembre; cartel con Los Viking's 5, Kuatreros del Sur y tributo a Tommy Rey (según anuncios de prensa).
- Fondas privadas tipo **Doña Florinda** o **Doña Rosa**: suelen manejar preventas escalonadas; revisa sus canales oficiales si buscas un formato más “cerrado” o VIP.

## Ideas de itinerario según tu estilo

**Itinerario “todo terreno” (viernes 18)**  
Mañana/tarde: Semana de la Chilenidad o fonda familiar · Noche: Parque O'Higgins (Jairo Vera / Noche de Brujas / Luis Lambis) o Ñuñoa según tu cartel favorito.

**Itinerario “familiar y económico” (sábado 19)**  
Día: Renca o Maipú · Tarde: cueca y juegos · Noche temprana: volver al centro antes del cierre del transporte.

**Itinerario “clásico dieciochero” (domingo 20)**  
Parque O'Higgins con Cachureos / Los Jaivas (jornada familiar hasta las 20:00) o cierre suave en Maipú.

## Cómo moverte y qué llevar

- Prioriza **metro y buses**; el tráfico dieciochero es lento.
- Lleva **cédula**, entradas digitales cargadas y efectivo o tarjeta para comida.
- Hidratación, protector solar de día y una capa abrigada para la noche.
- Si sales hasta tarde el 18 o 19, planifica el regreso con anticipación.

## Dónde hospedarte para vivir el 18 sin estrés

Si vienes de visita o quieres dormir cerca del centro histórico, **Black Cat Hostal Boutique** en **Compañía de Jesús 1921, Barrio Brasil**, es una buena base: estás a poca distancia de metro, bares y restaurantes del barrio, y con acceso cómodo hacia Parque O'Higgins, Ñuñoa o La Reina según el día.

¿Quieres armar tu plan del 18 con nosotros? Escríbenos por WhatsApp o reserva tu habitación y te orientamos con horarios de check-in y tips del barrio.

*Nota: precios, artistas y horarios corresponden a información pública disponible a inicios de septiembre 2026. Verifica siempre en Ticketplus, Ticketpro, somosrenca.cl y sitios municipales antes de comprar.*
""".strip()


def main() -> None:
    with httpx.Client(base_url=API, timeout=60.0, follow_redirects=True) as client:
        login = client.post(
            "/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        # Avoid duplicate slug
        existing = client.get("/posts/", headers=headers).json()
        for post in existing:
            if post.get("slug") == SLUG:
                resp = client.put(
                    f"/posts/{post['id']}",
                    headers=headers,
                    json={
                        "title": TITLE,
                        "excerpt": EXCERPT,
                        "body": BODY,
                        "category": "Turismo",
                        "author": "Black Cat Hostal",
                        "is_active": True,
                        "published_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                print(f"updated id={data['id']} slug={data['slug']}")
                return

        payload = {
            "slug": SLUG,
            "title": TITLE,
            "excerpt": EXCERPT,
            "body": BODY,
            "category": "Turismo",
            "image_url": "",
            "author": "Black Cat Hostal",
            "sort_order": 0,
            "is_active": True,
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
        resp = client.post("/posts/", headers=headers, json=payload)
        if resp.status_code >= 400:
            raise SystemExit(f"FAIL {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        print(f"created id={data['id']} slug={data['slug']}")
        print(f"url https://blackcathostal.com/post/{data['slug']}")


if __name__ == "__main__":
    main()
