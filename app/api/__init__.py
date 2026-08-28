from fastapi import APIRouter

from app.api import (
    ai,
    auth,
    campaigns,
    contact_groups,
    contacts,
    health,
    inquiries,
    items,
    mail_accounts,
    media,
    places,
    posts,
    reviews,
    rooms,
    services,
    sliders,
)

api_router = APIRouter()
api_router.include_router(ai.router)
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
api_router.include_router(places.router)
api_router.include_router(inquiries.router)
