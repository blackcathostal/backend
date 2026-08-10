from fastapi import APIRouter

from app.api import (
    auth,
    campaigns,
    contact_groups,
    contacts,
    health,
    items,
    mail_accounts,
    media,
    posts,
    reviews,
    rooms,
    services,
    sliders,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(sliders.router)
api_router.include_router(posts.router)
api_router.include_router(media.router)
api_router.include_router(contact_groups.router)
api_router.include_router(contacts.router)
api_router.include_router(mail_accounts.router)
api_router.include_router(campaigns.router)
api_router.include_router(services.router)
api_router.include_router(rooms.router)
api_router.include_router(items.router)
api_router.include_router(reviews.router)
