from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.models.contact_groups import ContactGroups
from app.models.contacts import Contacts
from app.models.hostel_info import CommonAnswers
from app.models.medias import Medias
from app.models.posts import Posts
from app.models.roles import Roles
from app.models.rooms import Rooms
from app.models.services import Services
from app.models.sliders import Sliders
from app.models.users import Users
from app.services.instagram_knowledge import get_or_create_hostel_info
from app.services.sync_sliders import sync_frontend_sliders

DEFAULT_SLIDERS = [
    {
        "eyebrow": "Santiago y Los Andes",
        "title": "Descubre la Ciudad de la Nieve",
        "image_url": "/uploads/sliders/frontend-santiago-1.webp",
        "overlay": 3,
        "sort_order": 1,
    },
    {
        "eyebrow": "Experiencia Valle Nevado",
        "title": "Vive la Magia de la Nieve",
        "image_url": "/uploads/sliders/frontend-santiago-2.webp",
        "overlay": 2,
        "sort_order": 2,
    },
    {
        "eyebrow": "Centro Histórico de Santiago",
        "title": "Explora la Ciudad Turística",
        "overlay": 3,
        "image_url": "/uploads/sliders/frontend-santiago-3.webp",
        "sort_order": 3,
    },
    {
        "eyebrow": "Vistas desde el San Cristóbal",
        "title": "Ciudad, Montañas y Cultura",
        "image_url": "/uploads/sliders/frontend-santiago-4.webp",
        "overlay": 3,
        "sort_order": 4,
    },
]

DEFAULT_POSTS = [
    {
        "slug": "descubre-el-historico-barrio-brasil",
        "title": "Descubre el histórico Barrio Brasil",
        "excerpt": (
            "Uno de los barrios con más historia y personalidad de Santiago. "
            "Sus calles esconden mansiones patrimoniales, arte urbano, cafés y una vida cultural "
            "que enamora a cada viajero que lo recorre."
        ),
        "body": (
            "El Barrio Brasil es uno de los sectores con más personalidad e historia de Santiago. "
            "Sus calles conservan mansiones patrimoniales de principios del siglo XX, hoy convertidas "
            "en cafés, restaurantes, galerías y espacios culturales que conviven con un vibrante arte urbano.\n\n"
            "A pasos de Black Cat Hostal encontrarás la Plaza Brasil, la Basílica del Salvador y una "
            "variada oferta gastronómica. Es el punto de partida ideal para explorar el casco antiguo de la ciudad."
        ),
        "category": "Barrio",
        "image_url": "/cappa/img/barrio/bb-plaza-1.webp",
        "author": "Black Cat Hostal",
        "sort_order": 1,
        "published_at": datetime(2026, 12, 2, tzinfo=timezone.utc),
    },
    {
        "slug": "que-hacer-en-santiago-en-3-dias",
        "title": "Qué hacer en Santiago en 3 días",
        "excerpt": (
            "Desde el cerro San Cristóbal hasta el centro histórico y sus museos, "
            "te contamos cómo aprovechar al máximo tu estadía en la capital chilena."
        ),
        "body": (
            "Santiago concentra cultura, gastronomía y naturaleza a poca distancia. "
            "En tres días puedes recorrer el centro histórico, subir al San Cristóbal, "
            "visitar museos y disfrutar de barrios como Lastarria, Brasil y Bellavista.\n\n"
            "Desde Black Cat Hostal te armamos un itinerario simple para que no pierdas tiempo "
            "y vivas lo mejor de la ciudad."
        ),
        "category": "Turismo",
        "image_url": "/cappa/img/santiago/teleferico.webp",
        "author": "Black Cat Hostal",
        "sort_order": 2,
        "published_at": datetime(2026, 12, 4, tzinfo=timezone.utc),
    },
    {
        "slug": "excursiones-a-la-nieve-cerca-de-la-ciudad",
        "title": "Excursiones a la nieve cerca de la ciudad",
        "excerpt": (
            "A pocas horas de Santiago encontrarás centros de montaña y valles nevados "
            "perfectos para vivir la magia de la nieve durante tu visita."
        ),
        "body": (
            "En temporada de invierno, Valle Nevado, Farellones y La Parva están a pocas horas "
            "del centro. Son opciones ideales para una escapada de día o un fin de semana en la nieve.\n\n"
            "Te ayudamos con tipología de traslados, horarios y recomendaciones para que salgas "
            "temprano y regreses a descansar en el hostal."
        ),
        "category": "Nieve",
        "image_url": "/cappa/img/santiago/2.webp",
        "author": "Black Cat Hostal",
        "sort_order": 3,
        "published_at": datetime(2026, 12, 6, tzinfo=timezone.utc),
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
        "status": "Disponible",
        "features": "Single bed, private bathroom, Wi-Fi, heating, continental breakfast included.",
    },
    {
        "name": "Double Room",
        "type": "Doble",
        "capacity": 2,
        "price": 48000,
        "status": "Disponible",
        "features": "Queen/double bed, private bathroom, Wi-Fi, heating, continental breakfast included.",
    },
    {
        "name": "Triple Room",
        "type": "Triple",
        "capacity": 3,
        "price": 62000,
        "status": "Ocupada",
        "features": "Twin/triple setup, private bathroom, Wi-Fi, heating, continental breakfast included.",
    },
    {
        "name": "Boutique Suite",
        "type": "Suite",
        "capacity": 2,
        "price": 79000,
        "status": "Disponible",
        "features": "Superior king bed, higher comfort, private bathroom, Wi-Fi, heating, continental breakfast included.",
    },
    {
        "name": "Family Room",
        "type": "Familiar",
        "capacity": 4,
        "price": 89000,
        "status": "Mantenimiento",
        "features": "Family/shared layout, multiple beds or bunks, private bathroom, Wi-Fi, heating, breakfast included.",
    },
]

DEFAULT_COMMON_ANSWERS = [
    {
        "topic": "prices",
        "keywords": "price,precio,cuanto,cuánto,rate,tarifa,cost,habitacion,habitación,room",
        "question": "How much does a room cost?",
        "answer_guide": "Call search_rooms and share from-prices in CLP by room type. Say rates are referential and may vary by season. Invite WhatsApp for exact dates.",
        "sort_order": 1,
    },
    {
        "topic": "checkin",
        "keywords": "arrival,llegada,check-in,checkin,entrada",
        "question": "What time is check-in?",
        "answer_guide": "Use get_hostel_info.check_in_time. Mention arrival instructions are sent before check-in.",
        "sort_order": 2,
    },
    {
        "topic": "checkout",
        "keywords": "departure,salida,check-out,checkout,mediodia,noon",
        "question": "What time is check-out?",
        "answer_guide": "Use check_out_time. Late check-out subject to availability; ask them to message WhatsApp.",
        "sort_order": 3,
    },
    {
        "topic": "breakfast",
        "keywords": "breakfast,desayuno,included,incluido,horario desayuno",
        "question": "Is breakfast included? What are the hours?",
        "answer_guide": "Yes, continental breakfast included. Hours from breakfast_hours in hostel info.",
        "sort_order": 4,
    },
    {
        "topic": "parking",
        "keywords": "parking,estacionamiento,auto,car,parqueo",
        "question": "Do you have parking?",
        "answer_guide": "Use has_parking and parking_notes. Do not invent space availability.",
        "sort_order": 5,
    },
    {
        "topic": "whatsapp",
        "keywords": "whatsapp,wsp,contact,contacto,reservar,book",
        "question": "How can I contact you / WhatsApp?",
        "answer_guide": "Share whatsapp_url and email from get_hostel_info.",
        "sort_order": 6,
    },
    {
        "topic": "location",
        "keywords": "where,donde,dónde,location,ubicacion,address,dirección,barrio brasil",
        "question": "Where are you located?",
        "answer_guide": "Share address and Barrio Brasil, Santiago. Offer WhatsApp for arrival help.",
        "sort_order": 7,
    },
    {
        "topic": "wifi",
        "keywords": "wifi,wi-fi,internet",
        "question": "Do you have Wi-Fi?",
        "answer_guide": "Yes, fiber Wi-Fi in rooms and common areas.",
        "sort_order": 8,
    },
    {
        "topic": "groups",
        "keywords": "group,grupo,crew,equipo,productora,corporate,corporativo",
        "question": "Can you host groups / production crews?",
        "answer_guide": "Yes, we work with groups and corporate stays. Ask for dates and headcount via WhatsApp to quote.",
        "sort_order": 9,
    },
    {
        "topic": "children",
        "keywords": "children,kids,niños,ninos,family,familia,edades",
        "question": "Until what age are guests considered children?",
        "answer_guide": "Under 12 years old. Ask ages when booking to assign the best room.",
        "sort_order": 10,
    },
    {
        "topic": "cancellation",
        "keywords": "cancel,cancelar,cancellation,cancelación,refund,reembolso",
        "question": "Can I cancel my booking?",
        "answer_guide": "Depends on booking terms. Escalate or ask them to write WhatsApp/email with booking code.",
        "sort_order": 11,
    },
    {
        "topic": "availability",
        "keywords": "available,disponible,availability,disponibilidad,dates,fechas",
        "question": "Do you have availability for these dates?",
        "answer_guide": "Do not confirm exact inventory on Instagram. Ask for dates via WhatsApp for human review.",
        "sort_order": 12,
    },
]


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

    if db.query(Posts).count() == 0:
        for item in DEFAULT_POSTS:
            db.add(Posts(**item, is_active=True))

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

    db.commit()
