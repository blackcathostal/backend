from fastapi import APIRouter

from app.api import (
    accounting_accounts,
    ai,
    analytics,
    audits,
    auth,
    campaigns,
    complaints,
    contact_groups,
    contacts,
    facebook,
    health,
    inquiries,
    instagram,
    items,
    logs,
    mail_accounts,
    media,
    places,
    posts,
    reports,
    reviews,
    roles,
    rooms,
    services,
    short_links,
    sliders,
    customers,
    suppliers,
    tax_documents,
    terms,
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
api_router.include_router(complaints.router)
api_router.include_router(services.router)
api_router.include_router(rooms.router)
api_router.include_router(items.router)
api_router.include_router(reviews.router)
api_router.include_router(places.router)
api_router.include_router(inquiries.router)
api_router.include_router(short_links.router)
api_router.include_router(instagram.router)
api_router.include_router(facebook.router)
api_router.include_router(analytics.router)
api_router.include_router(terms.router)
api_router.include_router(accounting_accounts.router)
api_router.include_router(reports.router)
api_router.include_router(tax_documents.router, prefix="/tax-documents")
# Legacy path for older admin builds
api_router.include_router(tax_documents.router, prefix="/accounting-track")
api_router.include_router(audits.router)
api_router.include_router(logs.router)
api_router.include_router(roles.router)
api_router.include_router(suppliers.router)
api_router.include_router(customers.router)
