from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.models.contact_groups import ContactGroups
from app.models.contacts import Contacts
from app.models.hostel_info import CommonAnswers
from app.models.medias import Medias
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
        "name": "Habitación Individual",
        "type": "Individual",
        "capacity": 1,
        "price": 35000,
        "features": "Cama individual, baño privado, Wi-Fi, calefacción, desayuno continental incluido.",
    },
    {
        "name": "Habitación Matrimonial",
        "type": "Matrimonial",
        "capacity": 2,
        "price": 54000,
        "features": "Cama matrimonial/queen, baño privado, Wi-Fi, desayuno continental incluido. Tarifa directa WhatsApp $54.000 CLP.",
    },
    {
        "name": "Habitación Triple",
        "type": "Triple",
        "capacity": 3,
        "price": 62000,
        "features": "Disposición twin/triple, baño privado, Wi-Fi, calefacción, desayuno continental incluido.",
    },
    {
        "name": "Suite Boutique",
        "type": "Suite",
        "capacity": 2,
        "price": 79000,
        "features": "Cama king superior, mayor confort, baño privado, Wi-Fi, calefacción, desayuno continental incluido.",
    },
    {
        "name": "Dormitorio Compartido",
        "type": "Dormitorio",
        "capacity": 6,
        "price": 15000,
        "features": (
            "Cama individual en dormitorio compartido. Capacidad 6 personas. "
            "Precio por persona/cama ($15.000 CLP), no por habitación completa. "
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
        "keywords": "tarifa,precio,whatsapp,matrimonial,dormitorio,king,queen,iva",
        "question": "¿Cuáles son las tarifas para reservas directas por WhatsApp?",
        "answer_guide": (
            "Matrimonial Cama King/Queen: $54.000 CLP/noche (Desayuno incluido). "
            "Dormitorio Compartido (4 o 6 camas): $15.000 CLP por cama. "
            "Todos los precios incluyen IVA."
        ),
        "sort_order": 3,
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
            "Presentando tu cédula de identidad/pasaporte vigente en recepción al momento del check-in "
            "durante el mes de tu cumpleaños."
        ),
        "sort_order": 5,
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
        "keywords": "dormitorio,compartido,cama,hombres,mujeres,sexo,4,6",
        "question": "¿Cómo funciona el dormitorio compartido?",
        "answer_guide": (
            "Es un solo tipo de habitación: dormitorio compartido de 4 o 6 camas. "
            "Se cobra $15.000 CLP por cama (por persona), no por la habitación completa. "
            "Las plazas se asignan en dormitorios solo hombres o solo mujeres. Los precios incluyen IVA."
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


def seed_database(db: Session) -> None:
    roles = {
        "admin": "Full access to the administration panel",
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
        role_by_name[name] = role

    admin = db.query(Users).filter(Users.email == settings.admin_email).first()
    if not admin:
        db.add(
            Users(
                role_id=role_by_name[settings.admin_role_name].id,
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

    if db.query(Rooms).count() == 0:
        for item in DEFAULT_ROOMS:
            db.add(Rooms(**item))
    else:
        # Backfill empty features on existing rooms once.
        for room in db.query(Rooms).all():
            if not (getattr(room, "features", None) or "").strip():
                match = next(
                    (r for r in DEFAULT_ROOMS if r["type"] == room.type),
                    None,
                )
                if match:
                    room.features = match["features"]

    get_or_create_hostel_info(db)

    if db.query(CommonAnswers).count() == 0:
        for item in DEFAULT_COMMON_ANSWERS:
            db.add(CommonAnswers(**item, is_active=True))
    else:
        by_topic = {item["topic"]: item for item in DEFAULT_COMMON_ANSWERS}
        for row in db.query(CommonAnswers).all():
            legacy = LEGACY_ENGLISH_ANSWERS.get(row.topic)
            updated = by_topic.get(row.topic)
            if not legacy or not updated:
                continue
            if row.question.strip() == legacy[0] and row.answer_guide.strip() == legacy[1]:
                row.question = updated["question"]
                row.answer_guide = updated["answer_guide"]
                row.keywords = updated["keywords"]

    db.commit()
    ensure_default_terms(db)
