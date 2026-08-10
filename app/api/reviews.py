from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services.google_photos import get_google_photos
from app.services.google_reviews import get_google_reviews
from app.services.tripadvisor_photo_import import import_traveler_photo_urls
from app.services.tripadvisor_photos import get_tripadvisor_photos
from app.services.tripadvisor_reviews import get_tripadvisor_reviews

router = APIRouter(prefix="/reviews", tags=["reviews"])


class TripadvisorPhotoImportBody(BaseModel):
    urls: list[str] = Field(default_factory=list, description="Tripadvisor media-cdn photo URLs")


@router.get("/google")
async def google_reviews(force: bool = Query(False, description="Bypass cache and refresh from Google")):
    return await get_google_reviews(force=force)


@router.get("/google/photos")
async def google_photos(
    force: bool = Query(False, description="Bypass cache and refresh Google visitor photos"),
):
    return await get_google_photos(force=force)


@router.get("/tripadvisor")
async def tripadvisor_reviews(
    force: bool = Query(False, description="Bypass cache and refresh from TripAdvisor"),
):
    return await get_tripadvisor_reviews(force=force)


@router.get("/tripadvisor/photos")
async def tripadvisor_photos(
    force: bool = Query(False, description="Bypass cache and refresh Tripadvisor traveler photos"),
):
    return await get_tripadvisor_photos(force=force)


@router.post("/tripadvisor/photos/import")
async def tripadvisor_photos_import(body: TripadvisorPhotoImportBody):
    """Import traveler photo CDN URLs (e.g. from bookmarklet on Tripadvisor album)."""
    return await import_traveler_photo_urls(body.urls)
