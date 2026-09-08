"""Create draft campaigns for Corporativo + Productoras (no send)."""

from __future__ import annotations

import httpx

API = "https://api.blackcathostal.com/api"
ADMIN_EMAIL = "admin@blackcathostal.com"
ADMIN_PASSWORD = "admin123"

CORPORATIVO_HTML = """
<p>Estimada Dirección Corporativa;</p>
<p>En <strong>Hostal Boutique Black Cat</strong> acompañamos la movilidad corporativa de empresas que buscan optimizar sus presupuestos de viaje sin sacrificar estándar, eficiencia ni confort para sus colaboradores.</p>
<p>Ubicados en un punto estratégico del centro histórico de Santiago y con conexión directa por autopista a 18 minutos del Aeropuerto AMB, ofrecemos una alternativa boutique eficiente para estadías de trabajo, consultorías, capacitaciones y movilidad de equipos.</p>
<p><strong>Atributos clave para su empresa:</strong></p>
<ul>
  <li><strong>Conectividad y eficiencia:</strong> Acceso rápido a los principales sectores comerciales de la capital y facilidades para el trabajo remoto.</li>
  <li><strong>Estándar boutique:</strong> Habitaciones privadas de alto confort orientadas al descanso efectivo del profesional.</li>
  <li><strong>Gestión simplificada:</strong> Facturación corporativa centralizada, procesos ágiles de check-in/out y canales de atención directa.</li>
</ul>
<p><strong>Beneficios del Convenio Corporativo:</strong></p>
<ul>
  <li><strong>Tarifas preferenciales fijas:</strong> Optimizamos su presupuesto operativo con precios preferenciales todo el año.</li>
  <li><strong>Políticas de cancelación flexibles:</strong> Flexibilidad adaptada a los cambios de agenda corporativos.</li>
  <li><strong>Atención prioritaria:</strong> Ejecutiva de cuenta dedicada para sus solicitudes.</li>
</ul>
<p>Adjunto nuestro <strong>Brochure Corporativo</strong> (<em>Black Cat 2024 Corporativo.pdf</em>) y el <strong>Tarifario de Convenio Empresa 2026/mayo 2027</strong> para su evaluación.</p>
<p>¿Tendrá disponibilidad para una breve llamada o reunión presencial y revisar las condiciones para su empresa?</p>
<p>Saluda,</p>
""".strip()

PRODUCTORAS_HTML = """
<p>Estimado equipo de Producción,</p>
<p>Les escribimos desde <strong>Hostal Boutique Black Cat</strong>. Sabemos que la logística de alojamiento para staff, artistas, talentos y crews en Santiago requiere flexibilidad, excelente conectividad y tarifas competitivas sin descuidar el confort.</p>
<p>Queremos ser su aliado operativo directo en el centro de Santiago para sus próximos montajes, festivales, conciertos y eventos corporativos.</p>
<p><strong>¿Por qué Black Cat es ideal para sus producciones?</strong></p>
<ul>
  <li><strong>Ubicación estratégica y logística:</strong> Conexión directa por autopista a solo 18 minutos del Aeropuerto AMB y acceso rápido a los principales recintos de eventos y conectividad con el casco histórico.</li>
  <li><strong>Formatos versátiles de habitación:</strong> Habitaciones privadas (ideales para directores, jefes de área o talentos) y compartidas de alto estándar boutique (perfectas para equipos técnicos y staff masivo).</li>
  <li><strong>Espacios funcionales:</strong> Áreas comunes amplias para breves reuniones de pauta, ambiente relajado para el descanso tras jornadas extensas y atención personalizada.</li>
</ul>
<p><strong>Beneficios para la Productora:</strong></p>
<ul>
  <li><strong>Tarifas especiales para producciones y grupos:</strong> Precios de convenio flexibles ajustados a la escala de su proyecto.</li>
  <li><strong>Atención prioritaria y bloqueos:</strong> Gestión ágil de reservas grupales y flexibilidad operativa ante cambios de agenda típicos del rubro.</li>
  <li><strong>Atención personalizada:</strong> Soporte continuo para coordinar check-ins masivos o en horarios especiales.</li>
</ul>
<p>Nos encantaría coordinar una breve reunión o recibir a su equipo de logística en nuestras instalaciones para que conozcan el espacio. Adjuntamos nuestro <strong>Brochure Comercial</strong> y el <strong>Tarifario Especial Producciones 2026/mayo2027</strong>.</p>
<p>¿Nos comentarían su disponibilidad para conversar brevemente sobre sus próximos proyectos?</p>
<p>Saluda,</p>
""".strip()

CAMPAIGNS = [
    {
        "name": "Corporativo – Convenio alojamiento 2026",
        "subject": "Solución de alojamiento corporativo para su empresa – Hostal Boutique Black Cat",
        "html_body": CORPORATIVO_HTML,
        "group_name": "Corporativo",
    },
    {
        "name": "Productoras – Alianza eventos 2026",
        "subject": "Alianza estratégica de alojamiento para sus eventos y producciones – Hostal Boutique Black Cat",
        "html_body": PRODUCTORAS_HTML,
        "group_name": "Productoras",
    },
]


def main() -> None:
    with httpx.Client(base_url=API, timeout=120.0, follow_redirects=True) as client:
        login = client.post(
            "/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        mail_accounts = client.get("/mail-accounts/?active_only=true", headers=headers)
        mail_accounts.raise_for_status()
        accounts = mail_accounts.json()
        if not accounts:
            raise SystemExit("No hay cuentas de correo activas")
        from_email = accounts[0]["email"]

        groups = {
            g["name"]: g["id"]
            for g in client.get("/contact-groups/", headers=headers).json()
        }

        existing = {
            (c.get("name") or "").strip().lower(): c
            for c in client.get("/campaigns/", headers=headers).json()
        }

        for spec in CAMPAIGNS:
            group_id = groups.get(spec["group_name"])
            if not group_id:
                raise SystemExit(f"Grupo no encontrado: {spec['group_name']}")

            contacts = client.get(
                f"/contacts/?group_id={group_id}&active_only=true",
                headers=headers,
            )
            contacts.raise_for_status()
            recipients = [
                {
                    "id": c["id"],
                    "full_name": c["full_name"],
                    "email": c["email"],
                }
                for c in contacts.json()
                if c.get("email")
            ]

            payload = {
                "name": spec["name"],
                "from_email": from_email,
                "subject": spec["subject"],
                "html_body": spec["html_body"],
                "status": "Borrador",
                "sent": 0,
                "recipients": recipients,
                "attachments": [],
            }

            key = spec["name"].strip().lower()
            if key in existing:
                camp_id = existing[key]["id"]
                resp = client.put(f"/campaigns/{camp_id}", headers=headers, json=payload)
                action = "updated"
            else:
                resp = client.post("/campaigns/", headers=headers, json=payload)
                action = "created"

            if resp.status_code >= 400:
                print(f"FAIL {spec['name']}: {resp.status_code} {resp.text[:300]}")
                continue

            data = resp.json()
            print(
                f"{action}: id={data['id']} | {spec['name']} | "
                f"recipients={len(recipients)} | status={data.get('status')}"
            )


if __name__ == "__main__":
    main()
