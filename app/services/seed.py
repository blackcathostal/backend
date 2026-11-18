from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.models.tax_documents import (
    DteStatuses,
    DteTypes,
    DteVersions,
    TaxDocuments,
)
from app.services import dte_catalog
from app.models.contact_groups import ContactGroups
from app.models.contacts import Contacts
from app.models.hostel_info import CommonAnswers
from app.models.medias import Medias
from app.models.permissions import Permissions, RolePermissions
from app.models.roles import Roles
from app.models.rooms import Rooms
from app.models.services import Services
from app.models.sliders import Sliders
from app.models.users import Users
from app.services.instagram_knowledge import get_or_create_hostel_info
from app.services.sync_sliders import sync_frontend_sliders
from app.services.terms import ensure_default_terms

DEFAULT_SLIDERS = [
    {
        "eyebrow": "Santiago y Los Andes",
        "title": "Descubre la Ciudad de la Nieve",
        "image_url": "/cappa/img/slider/santiago/1.webp",
        "overlay": 3,
        "sort_order": 1,
    },
    {
        "eyebrow": "Experiencia Valle Nevado",
        "title": "Vive la Magia de la Nieve",
        "image_url": "/cappa/img/slider/santiago/2.webp",
        "overlay": 2,
        "sort_order": 2,
    },
    {
        "eyebrow": "Centro Histórico de Santiago",
        "title": "Explora la Ciudad Turística",
        "overlay": 3,
        "image_url": "/cappa/img/slider/santiago/3.webp",
        "sort_order": 3,
    },
    {
        "eyebrow": "Vistas desde el San Cristóbal",
        "title": "Ciudad, Montañas y Cultura",
        "image_url": "/cappa/img/slider/santiago/4.webp",
        "overlay": 3,
        "sort_order": 4,
    },
]

DEFAULT_SERVICES = [
    {"name": "Desayuno incluido", "category": "Gastronomía", "price": "Incluido", "status": "Activo"},
    {"name": "Traslado aeropuerto", "category": "Transporte", "price": "$18.000", "status": "Activo"},
    {"name": "Tour Valle Nevado", "category": "Turismo", "price": "$45.000", "status": "Activo"},
    {"name": "Lavandería", "category": "Hostal", "price": "$6.000", "status": "Activo"},
    {"name": "Alquiler bicicletas", "category": "Recreación", "price": "$8.000", "status": "Inactivo"},
]

DEFAULT_ROOMS = [
    {
        "name": "Habitación Matrimonial",
        "type": "Matrimonial",
        "capacity": 2,
        "price": 54990,
        "features": "Cama matrimonial/queen, baño privado, Wi-Fi, desayuno continental incluido. Tarifa desde $54.990 CLP.",
    },
    {
        "name": "Habitación Twin",
        "type": "Twin",
        "capacity": 2,
        "price": 54990,
        "features": "Dos camas, baño privado, Wi-Fi, desayuno continental incluido. Tarifa desde $54.990 CLP.",
    },
    {
        "name": "Habitación Triple",
        "type": "Triple",
        "capacity": 3,
        "price": 69990,
        "features": "Triple, baño privado, Wi-Fi, desayuno continental incluido. Tarifa desde $69.990 CLP.",
    },
    {
        "name": "Suite Boutique",
        "type": "Suite",
        "capacity": 2,
        "price": 54990,
        "features": "Suite, baño privado, Wi-Fi, desayuno continental incluido. Tarifa desde $54.990 CLP.",
    },
    {
        "name": "Dormitorio Compartido",
        "type": "Dormitorio",
        "capacity": 6,
        "price": 19990,
        "features": (
            "Cama individual en dormitorio compartido. "
            "Precio por persona/cama ($19.990 CLP), no por habitación completa. "
            "Wi-Fi y desayuno continental incluidos."
        ),
    },
]

DEFAULT_COMMON_ANSWERS = [
    {
        "topic": "checkin",
        "keywords": "check-in,checkin,check-out,checkout,horario,llegada,salida,recepción,24/7",
        "question": "¿Cuáles son los horarios de Check-in y Check-out?",
        "answer_guide": (
            "Check-in desde las 14:00 hrs / Check-out hasta las 12:00 hrs (mediodía). "
            "Recepción disponible 24/7."
        ),
        "sort_order": 1,
    },
    {
        "topic": "flexibility",
        "keywords": "early,late,check-out,checkout,antes,después,18:00,disponibilidad",
        "question": "¿Puedo ingresar antes o salir después del horario establecido?",
        "answer_guide": (
            "Early Check-in y Late Check-out están sujetos a disponibilidad previa coordinación. "
            "El Late Check-out posterior a las 12:00 y hasta las 18:00 hrs tiene un cargo de $20.000 CLP. "
            "Posterior a las 18:00 hrs aplica cobro del 100% de la tarifa diaria."
        ),
        "sort_order": 2,
    },
    {
        "topic": "tarifas",
        "keywords": "tarifa,precio,precios,matrimonial,suite,twin,triple,dormitorio,compartida,cama,noche,iva",
        "question": "¿Cuáles son las tarifas referenciales de las habitaciones?",
        "answer_guide": (
            "Tarifas referenciales por noche (pueden variar por temporada): "
            "Matrimonial o Suite $54.990; Twin $54.990; Triple $69.990; "
            "compartida por cama $19.990. Desayuno continental incluido."
        ),
        "sort_order": 3,
    },
    {
        "topic": "mensual",
        "keywords": "mensual,mes,mensualidad,por mes,long stay,estadía larga,estadia larga,1500000,1.500.000",
        "question": "¿Cuánto cuesta la habitación mensual?",
        "answer_guide": "La tarifa mensual de habitación es $1.500.000 CLP.",
        "sort_order": 31,
    },
    {
        "topic": "fans",
        "keywords": "fans,beneficio,descuento,ota,espumante,cumpleaños,whatsapp",
        "question": "¿Qué beneficios exclusivos reciben los Black Cat Fans?",
        "answer_guide": (
            "Descuento respecto a OTAs, opción de espumante de cortesía por reserva dentro del plazo "
            "de promoción, horarios flexibles según disponibilidad y hasta 50% de descuento en estadía "
            "el mes de cumpleaños."
        ),
        "sort_order": 4,
    },
    {
        "topic": "birthday",
        "keywords": "cumpleaños,beneficio,cédula,pasaporte,fans,descuento",
        "question": "¿Cómo aplico el beneficio de cumpleaños?",
        "answer_guide": (
            "El beneficio de cumpleaños se confirma por WhatsApp. "
            "En check-in presentas cédula o pasaporte vigente del mes de cumpleaños."
        ),
        "sort_order": 5,
    },
    {
        "topic": "voluntariado",
        "keywords": "voluntariado,voluntario,volunteer,workaway,gratis,trabajo gratis,helpxx",
        "question": "¿Aceptan voluntariado o trabajo a cambio de alojamiento?",
        "answer_guide": "Por ahora no estamos aceptando voluntariado.",
        "sort_order": 32,
    },
    {
        "topic": "services",
        "keywords": "servicios,incluye,desayuno,wifi,cocina,cafetería,piscina,equipaje,bicicleta",
        "question": "¿Qué servicios incluye la estadía?",
        "answer_guide": (
            "Desayuno continental, Wifi gratuito, cocina comunitaria equipada, "
            "cafetería (8:00 a 11:30), piscina, zonas para fumadores, "
            "guardería de equipaje y arriendo de bicicletas."
        ),
        "sort_order": 6,
    },
    {
        "topic": "parking",
        "keywords": "estacionamiento,parking,auto,cupos",
        "question": "¿Cuentan con estacionamiento?",
        "answer_guide": (
            "Sí, disponemos de estacionamiento con cupos limitados, sujeto a confirmación previa."
        ),
        "sort_order": 7,
    },
    {
        "topic": "smoking",
        "keywords": "fumar,humo,multa,patio,fumadores,smoking",
        "question": "¿Se permite fumar en el hostal?",
        "answer_guide": (
            "Está estrictamente prohibido fumar dentro de las habitaciones (sujeto a multa). "
            "Existen áreas designadas para fumadores en los patios y zonas al aire libre."
        ),
        "sort_order": 8,
    },
    {
        "topic": "diet",
        "keywords": "desayuno,vegetariano,vegano,gluten,alergia,cafetería",
        "question": "¿El desayuno contempla opciones vegetarianas, veganas o sin gluten?",
        "answer_guide": (
            "Contamos con opciones adaptables como leche vegetal y frutas. "
            "Solicitamos notificar alergias o restricciones al reservar."
        ),
        "sort_order": 9,
    },
    {
        "topic": "luggage",
        "keywords": "equipaje,custodia,guardería,check-in,check-out,maleta",
        "question": "¿Tienen custodia de equipaje antes del check-in o después del check-out?",
        "answer_guide": (
            "Sí, ofrecemos servicio de guardería de equipaje sin costo adicional "
            "para que disfrutes tu día en Santiago."
        ),
        "sort_order": 10,
    },
    {
        "topic": "location",
        "keywords": "dónde,donde,ubicación,dirección,metro,cumming,república,barrio brasil",
        "question": "¿Dónde están ubicados y cómo llegar?",
        "answer_guide": (
            "Compañía de Jesús #1921, Barrio Brasil, Santiago Centro. "
            "A pasos de estación de Metro Cumming (Línea 5) y República (Línea 1)."
        ),
        "sort_order": 11,
    },
    {
        "topic": "groups",
        "keywords": "grupo,rooming,anticipo,pago,anulación,cancelación,penalidad",
        "question": "¿Cuáles son las condiciones para reservas de grupos y forma de pago?",
        "answer_guide": (
            "Requiere envío de Rooming List 72 hrs antes. Anticipo del 50% al reservar "
            "(30 días antes) y saldo del 50% al Check-in. Anulación sin costo hasta 30 días antes; "
            "menos de 30 días aplica penalidad del 50%."
        ),
        "sort_order": 12,
    },
    {
        "topic": "corporate",
        "keywords": "empresa,corporativo,convenio,factura,iva,exenta",
        "question": "¿Tienen convenios corporativos o tarifas para empresas?",
        "answer_guide": (
            "Sí, ofrecemos tarifas preferenciales y facturación exenta o con IVA según requerimiento. "
            "Contactar a reservas@blackcathostal.com."
        ),
        "sort_order": 13,
    },
    {
        "topic": "payment",
        "keywords": "pago,transferencia,tarjeta,efectivo,usd,clp,débito,crédito",
        "question": "¿Cuáles son los métodos de pago aceptados?",
        "answer_guide": (
            "Transferencia bancaria (CLP/USD), tarjetas de débito/crédito y efectivo en recepción."
        ),
        "sort_order": 14,
    },
    {
        "topic": "dormitory",
        "keywords": "dormitorio,compartido,compartida,cama,hombres,mujeres,sexo,4,6",
        "question": "¿Cómo funciona el dormitorio compartido?",
        "answer_guide": (
            "Dormitorio compartido: se cobra $19.990 CLP por cama (por persona), "
            "no por la habitación completa. "
            "Tenemos compartidas solo para hombres y solo para mujeres; "
            "pregunta cuál requiere (no preguntes orientación sexual "
            "ni fuerces «¿eres hombre o mujer?»). "
            "Para reservar una cama, escribir por WhatsApp con fechas."
        ),
        "sort_order": 15,
    },
    {
        "topic": "breakfast",
        "keywords": "desayuno,incluido,horario,cafetería,breakfast",
        "question": "¿El desayuno está incluido? ¿En qué horario?",
        "answer_guide": (
            "Sí, el desayuno continental está incluido. La cafetería atiende de 08:00 a 11:30 hrs."
        ),
        "sort_order": 16,
    },
]

LEGACY_ENGLISH_ANSWERS: dict[str, tuple[str, str]] = {}


DEFAULT_SIDEBAR_PERMISSIONS = (
    ("dashboard", "Inicio", "dashboard", "Access dashboard"),
    ("accounting", "Contabilidad", "accounting", "Accounting section"),
    ("track", "Track", "track", "Tax documents Track section"),
    ("sliders", "Sliders", "sliders", "Sliders section"),
    ("rooms", "Habitaciones", "rooms", "Rooms section"),
    ("services", "Servicios", "services", "Services section"),
    ("complaints", "Denuncias", "complaints", "Complaints section"),
    ("analytics", "Visitas", "analytics", "Analytics section"),
    ("blog", "Blog", "blog", "Blog section"),
    ("communications", "Comunicaciones", "communications", "Communications section"),
    ("settings", "Configuraciones", "settings", "Settings section"),
    ("maintainers", "Mantenedores", "maintainers", "Maintainers section"),
    ("suppliers", "Proveedores", "maintainers", "Suppliers list"),
    ("roles", "Roles", "maintainers", "Roles and permissions management"),
)


def seed_database(db: Session) -> None:
    roles = {
        "admin": "Full access to the administration panel (legacy)",
        "Administrador": "Full access to the administration panel",
        "Comunicador": "Communications-focused access",
        "editor": "Can manage content such as sliders, posts and media",
        "viewer": "Read-only access",
    }
    role_by_name: dict[str, Roles] = {}
    for name, description in roles.items():
        role = db.query(Roles).filter(Roles.name == name).first()
        if not role:
            role = Roles(name=name, description=description)
            db.add(role)
            db.flush()
        else:
            role.description = description
        role_by_name[name] = role

    permission_by_code: dict[str, Permissions] = {}
    for code, label, section, description in DEFAULT_SIDEBAR_PERMISSIONS:
        row = db.query(Permissions).filter(Permissions.code == code).first()
        if row is None:
            row = Permissions(code=code, label=label, section=section, description=description)
            db.add(row)
            db.flush()
        else:
            row.label = label
            row.section = section
            row.description = description
        permission_by_code[code] = row

    db.flush()

    all_permission_ids = [p.id for p in permission_by_code.values()]
    communicator_codes = {
        "dashboard",
        "communications",
        "blog",
        "settings",
    }

    def ensure_role_permissions(role: Roles, codes: set[str] | None = None) -> None:
        wanted = (
            all_permission_ids
            if codes is None
            else [permission_by_code[c].id for c in codes if c in permission_by_code]
        )
        existing = {
            link.permission_id
            for link in db.query(RolePermissions).filter(RolePermissions.role_id == role.id).all()
        }
        for permission_id in wanted:
            if permission_id not in existing:
                db.add(RolePermissions(role_id=role.id, permission_id=permission_id))

    ensure_role_permissions(role_by_name["Administrador"])
    ensure_role_permissions(role_by_name["admin"])
    ensure_role_permissions(role_by_name["Comunicador"], communicator_codes)

    admin_role = role_by_name.get(settings.admin_role_name) or role_by_name["Administrador"]
    admin = db.query(Users).filter(Users.email == settings.admin_email).first()
    if not admin:
        db.add(
            Users(
                role_id=admin_role.id,
                email=settings.admin_email,
                full_name=settings.admin_full_name,
                password=hash_password(settings.admin_password),
                is_active=True,
            )
        )

    if db.query(Sliders).count() == 0:
        for item in DEFAULT_SLIDERS:
            db.add(Sliders(**item, is_active=True))

    if db.query(Medias).count() == 0:
        for item in DEFAULT_SLIDERS:
            db.add(
                Medias(
                    filename=item["image_url"].split("/")[-1],
                    url=item["image_url"],
                    category="slider",
                    alt_text=item["title"],
                )
            )

    # Always keep the known frontend homepage sliders synced into DB + uploads.
    sync_frontend_sliders(db)

    agencias = (
        db.query(ContactGroups)
        .filter(ContactGroups.name == "Agencias")
        .first()
    )
    if not agencias:
        agencias = ContactGroups(
            name="Agencias",
            description="Agencias de viaje y operadores",
            is_active=True,
        )
        db.add(agencias)
        db.flush()

    if db.query(Contacts).count() == 0:
        for item in (
            {"full_name": "Maria Perez", "email": "maria@email.com"},
            {"full_name": "Juan Soto", "email": "juan@email.com"},
            {"full_name": "Ana Ruiz", "email": "ana@email.com"},
        ):
            db.add(Contacts(**item, is_active=True, group_id=agencias.id))

    if db.query(Services).count() == 0:
        for item in DEFAULT_SERVICES:
            db.add(Services(**item))

    # Rooms CRUD in admin is the source of truth — seed only when empty.
    if db.query(Rooms).count() == 0:
        for item in DEFAULT_ROOMS:
            db.add(Rooms(**item))

    get_or_create_hostel_info(db)

    # Official price FAQ topics must stay in sync on every seed/startup.
    FORCE_FAQ_TOPICS = {"tarifas", "dormitory", "mensual", "voluntariado", "birthday"}
    if db.query(CommonAnswers).count() == 0:
        for item in DEFAULT_COMMON_ANSWERS:
            db.add(CommonAnswers(**item, is_active=True))
    else:
        by_topic = {item["topic"]: item for item in DEFAULT_COMMON_ANSWERS}
        existing_topics = {row.topic for row in db.query(CommonAnswers).all()}
        for item in DEFAULT_COMMON_ANSWERS:
            if item["topic"] not in existing_topics:
                db.add(CommonAnswers(**item, is_active=True))
        for row in db.query(CommonAnswers).all():
            updated = by_topic.get(row.topic)
            if not updated:
                continue
            if row.topic in FORCE_FAQ_TOPICS:
                row.question = updated["question"]
                row.answer_guide = updated["answer_guide"]
                row.keywords = updated["keywords"]
                continue
            legacy = LEGACY_ENGLISH_ANSWERS.get(row.topic)
            if not legacy:
                continue
            if row.question.strip() == legacy[0] and row.answer_guide.strip() == legacy[1]:
                row.question = updated["question"]
                row.answer_guide = updated["answer_guide"]
                row.keywords = updated["keywords"]

    # Catalog rows live in DB; seed only upserts by code (SII ids for types).
    dte_versions = (
        (1, dte_catalog.VERSION_CODE_ISSUED, "Issued"),
        (2, dte_catalog.VERSION_CODE_RECEIVED, "Received"),
    )
    for version_id, code, name in dte_versions:
        row = db.query(DteVersions).filter(DteVersions.code == code).first()
        if row is None:
            row = db.query(DteVersions).filter(DteVersions.id == version_id).first()
        if row is None:
            db.add(DteVersions(id=version_id, code=code, name=name))
        else:
            row.code = code
            row.name = name

    dte_types = (
        (33, dte_catalog.TYPE_CODE_INVOICE, "Factura"),
        (34, dte_catalog.TYPE_CODE_EXEMPT_INVOICE, "Factura Exenta"),
        (39, dte_catalog.TYPE_CODE_RECEIPT, "Boleta"),
        (61, dte_catalog.TYPE_CODE_CREDIT_NOTE, "Nota de crédito"),
    )
    for type_id, code, name in dte_types:
        row = db.query(DteTypes).filter(DteTypes.code == code).first()
        if row is None:
            row = db.query(DteTypes).filter(DteTypes.id == type_id).first()
        if row is None:
            db.add(DteTypes(id=type_id, code=code, name=name))
        else:
            row.code = code
            row.name = name

    dte_statuses = (
        (1, dte_catalog.STATUS_CODE_PENDING_PAYMENT, "Imputada por Pagar", "red"),
        (2, dte_catalog.STATUS_CODE_PAID, "Imputada Pagada", "green"),
        (3, dte_catalog.STATUS_CODE_WAITING, "En espera", "amber"),
    )
    for status_id, code, name, tip_color in dte_statuses:
        row = db.query(DteStatuses).filter(DteStatuses.code == code).first()
        if row is None:
            row = db.query(DteStatuses).filter(DteStatuses.id == status_id).first()
        if row is None:
            db.add(
                DteStatuses(
                    id=status_id,
                    code=code,
                    name=name,
                    tip_color=tip_color,
                )
            )
        else:
            row.code = code
            row.name = name
            row.tip_color = tip_color

    db.flush()

    issued_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_ISSUED)
    received_id = dte_catalog.version_id_by_code(db, dte_catalog.VERSION_CODE_RECEIVED)
    invoice_id = dte_catalog.type_id_by_code(db, dte_catalog.TYPE_CODE_INVOICE)
    receipt_id = dte_catalog.type_id_by_code(db, dte_catalog.TYPE_CODE_RECEIPT)
    default_status_id = dte_catalog.default_new_document_status_id(db)
    known_type_ids = dte_catalog.all_type_ids(db)

    # Backfill version/type/status; period stays empty unless already set.
    for doc in db.query(TaxDocuments).all():
        direction = str(getattr(doc, "direction", "") or "").strip().lower()
        if not getattr(doc, "dte_version_id", None):
            doc.dte_version_id = (
                received_id if direction in {"recibida", "received"} else issued_id
            ) or issued_id
        raw_type = str(getattr(doc, "doc_type", "") or "").strip().lower()
        type_id = getattr(doc, "dte_type_id", None)
        if type_id not in known_type_ids:
            resolved = dte_catalog.type_id_from_raw(db, raw_type or type_id)
            if resolved:
                doc.dte_type_id = resolved
            elif "boleta" in raw_type or "receipt" in raw_type or str(type_id) == dte_catalog.TYPE_CODE_RECEIPT:
                doc.dte_type_id = receipt_id
            else:
                doc.dte_type_id = invoice_id
        if not doc.status_id and default_status_id:
            doc.status_id = default_status_id
        if doc.period is None:
            doc.period = ""

    db.flush()

    # Remove legacy issued/received rows mistakenly stored in dte_types.
    for legacy in db.query(DteTypes).filter(
        DteTypes.code.in_(["issued", "received", "emitido", "recibido"])
    ).all():
        db.delete(legacy)

    db.commit()
    ensure_default_terms(db)
    # Refresh packaged Instagram rates cache when defaults are newer.
    from app.services.room_rates import load_room_rates

    load_room_rates()
