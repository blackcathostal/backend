from fastapi import APIRouter, Query

from app.services.google_nearby_restaurants import get_nearby_restaurants

router = APIRouter(prefix="/places", tags=["places"])


@router.get("/nearby-restaurants")
async def nearby_restaurants(locale: str = Query("es", description="Language code: es, en, pt")):
    return await get_nearby_restaurants(locale=locale)
