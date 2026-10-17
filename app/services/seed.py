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
        "name": "Single Room",
        "type": "Individual",
        "capacity": 1,
        "price": 35000,
        "features": "Single bed, private bathroom, Wi-Fi, heating, continental breakfast included.",
    },
    {
        "name": "Double Room",
        "type": "Doble",
        "capacity": 2,
        "price": 48000,
        "features": "Queen/double bed, private bathroom, Wi-Fi, heating, continental breakfast included.",
    },
    {
        "name": "Triple Room",
        "type": "Triple",
        "capacity": 3,
        "price": 62000,
        "features": "Twin/triple setup, private bathroom, Wi-Fi, heating, continental breakfast included.",
    },
    {
        "name": "Boutique Suite",
        "type": "Suite",
        "capacity": 2,
        "price": 79000,
        "features": "Superior king bed, higher comfort, private bathroom, Wi-Fi, heating, continental breakfast included.",
    },
    {
        "name": "Family Room",
        "type": "Familiar",
        "capacity": 4,
        "price": 89000,
        "features": "Family/shared layout, multiple beds or bunks, private bathroom, Wi-Fi, heating, breakfast included.",
    },
]

DEFAULT_COMMON_ANSWERS = [
    {
        "topic": "prices",
        "keywords": "precio,cuanto,cuánto,tarifa,costo,habitacion,habitación,room,price",
        "question": "¿Cuánto cuesta una habitación?",
        "answer_guide": "Consulta las habitaciones y entrega el precio desde, en CLP, según el tipo. Las tarifas son referenciales y pueden variar por temporada. Para fechas exactas, invita a escribir por WhatsApp.",
        "sort_order": 1,
    },
    {
        "topic": "checkin",
        "keywords": "llegada,check-in,checkin,entrada,arrival",
        "question": "¿A qué hora es el check-in?",
        "answer_guide": "Usa el horario de llegada del hostal. Las instrucciones de llegada se envían antes del check-in.",
        "sort_order": 2,
    },
    {
        "topic": "checkout",
        "keywords": "salida,check-out,checkout,mediodía,departure",
        "question": "¿A qué hora es el check-out?",
        "answer_guide": "Usa el horario de salida del hostal. La salida tardía depende de disponibilidad; pide que escriban por WhatsApp.",
        "sort_order": 3,
    },
    {
        "topic": "breakfast",
        "keywords": "desayuno,incluido,horario,breakfast",
        "question": "¿El desayuno está incluido? ¿En qué horario?",
        "answer_guide": "Sí, el desayuno continental está incluido. El horario es el que está guardado en la información del hostal.",
        "sort_order": 4,
    },
    {
        "topic": "parking",
        "keywords": "estacionamiento,auto,parqueo,parking",
        "question": "¿Tienen estacionamiento?",
        "answer_guide": "Usa si hay estacionamiento y las notas de estacionamiento. No inventes disponibilidad de cupos.",
        "sort_order": 5,
    },
    {
        "topic": "whatsapp",
        "keywords": "whatsapp,wsp,contacto,reservar,contact",
        "question": "¿Cómo los contacto? ¿Cuál es el WhatsApp?",
        "answer_guide": "Comparte el WhatsApp y el correo de la información del hostal.",
        "sort_order": 6,
    },
    {
        "topic": "location",
        "keywords": "dónde,donde,ubicación,dirección,barrio brasil,location",
        "question": "¿Dónde están ubicados?",
        "answer_guide": "Comparte la dirección en Barrio Brasil, Santiago. Ofrece el WhatsApp si necesitan ayuda para llegar.",
        "sort_order": 7,
    },
    {
        "topic": "wifi",
        "keywords": "wifi,wi-fi,internet",
        "question": "¿Tienen Wi-Fi?",
        "answer_guide": "Sí, hay Wi-Fi de fibra en las habitaciones y en las áreas comunes.",
        "sort_order": 8,
    },
    {
        "topic": "groups",
        "keywords": "grupo,equipo,productora,corporativo,group",
        "question": "¿Reciben grupos o equipos de producción?",
        "answer_guide": "Sí, trabajamos con grupos y estadías corporativas. Pide fechas y cantidad de personas por WhatsApp para cotizar.",
        "sort_order": 9,
    },
    {
        "topic": "children",
        "keywords": "niños,ninos,familia,edades,children",
        "question": "¿Hasta qué edad se considera niño?",
        "answer_guide": "Menores de 12 años. Pide las edades al reservar para asignar la habitación más adecuada.",
        "sort_order": 10,
    },
    {
        "topic": "cancellation",
        "keywords": "cancelar,cancelación,reembolso,cancel",
        "question": "¿Puedo cancelar mi reserva?",
        "answer_guide": "Depende de las condiciones de la reserva. Pide que escriban por WhatsApp o correo con el código de reserva.",
        "sort_order": 11,
    },
    {
        "topic": "availability",
        "keywords": "disponible,disponibilidad,fechas,available",
        "question": "¿Hay disponibilidad para estas fechas?",
        "answer_guide": "No confirmes cupos exactos por Instagram. Pide las fechas por WhatsApp para que una persona lo revise.",
        "sort_order": 12,
    },
]

LEGACY_ENGLISH_ANSWERS = {
    "prices": (
        "How much does a room cost?",
        "Call search_rooms and share from-prices in CLP by room type. Say rates are referential and may vary by season. Invite WhatsApp for exact dates.",
    ),
    "checkin": (
        "What time is check-in?",
        "Use get_hostel_info.check_in_time. Mention arrival instructions are sent before check-in.",
    ),
    "checkout": (
        "What time is check-out?",
        "Use check_out_time. Late check-out subject to availability; ask them to message WhatsApp.",
    ),
    "breakfast": (
        "Is breakfast included? What are the hours?",
        "Yes, continental breakfast included. Hours from breakfast_hours in hostel info.",
    ),
    "parking": (
        "Do you have parking?",
        "Use has_parking and parking_notes. Do not invent space availability.",
    ),
    "whatsapp": (
        "How can I contact you / WhatsApp?",
        "Share whatsapp_url and email from get_hostel_info.",
    ),
    "location": (
        "Where are you located?",
        "Share address and Barrio Brasil, Santiago. Offer WhatsApp for arrival help.",
    ),
    "wifi": (
        "Do you have Wi-Fi?",
        "Yes, fiber Wi-Fi in rooms and common areas.",
    ),
    "groups": (
        "Can you host groups / production crews?",
        "Yes, we work with groups and corporate stays. Ask for dates and headcount via WhatsApp to quote.",
    ),
    "children": (
        "Until what age are guests considered children?",
        "Under 12 years old. Ask ages when booking to assign the best room.",
    ),
    "cancellation": (
        "Can I cancel my booking?",
        "Depends on booking terms. Escalate or ask them to write WhatsApp/email with booking code.",
    ),
    "availability": (
        "Do you have availability for these dates?",
        "Do not confirm exact inventory on Instagram. Ask for dates via WhatsApp for human review.",
    ),
}


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
