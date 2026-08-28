from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.models.contact_groups import ContactGroups
from app.models.contacts import Contacts
from app.models.medias import Medias
from app.models.roles import Roles
from app.models.rooms import Rooms
from app.models.services import Services
from app.models.sliders import Sliders
from app.models.users import Users
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

DEFAULT_SERVICES = [
    {"name": "Desayuno incluido", "category": "Gastronomía", "price": "Incluido", "status": "Activo"},
    {"name": "Traslado aeropuerto", "category": "Transporte", "price": "$18.000", "status": "Activo"},
    {"name": "Tour Valle Nevado", "category": "Turismo", "price": "$45.000", "status": "Activo"},
    {"name": "Lavandería", "category": "Hostal", "price": "$6.000", "status": "Activo"},
    {"name": "Alquiler bicicletas", "category": "Recreación", "price": "$8.000", "status": "Inactivo"},
]

DEFAULT_ROOMS = [
    {"name": "Habitación Individual", "type": "Individual", "capacity": 1, "price": 35000, "status": "Disponible"},
    {"name": "Habitación Doble", "type": "Doble", "capacity": 2, "price": 48000, "status": "Disponible"},
    {"name": "Habitación Triple", "type": "Triple", "capacity": 3, "price": 62000, "status": "Ocupada"},
    {"name": "Suite Boutique", "type": "Suite", "capacity": 2, "price": 79000, "status": "Disponible"},
    {"name": "Habitación Familiar", "type": "Familiar", "capacity": 4, "price": 89000, "status": "Mantenimiento"},
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

    db.commit()
