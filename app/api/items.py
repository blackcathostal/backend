from fastapi import APIRouter, HTTPException

from app.schemas.item import ItemCreate, ItemRead, ItemUpdate
from app.services import items as items_service

router = APIRouter(prefix="/items", tags=["items"])


@router.get("/", response_model=list[ItemRead])
def list_items() -> list[ItemRead]:
    return items_service.list_items()


@router.get("/{item_id}", response_model=ItemRead)
def get_item(item_id: int) -> ItemRead:
    item = items_service.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@router.post("/", response_model=ItemRead, status_code=201)
def create_item(payload: ItemCreate) -> ItemRead:
    return items_service.create_item(payload)


@router.put("/{item_id}", response_model=ItemRead)
def update_item(item_id: int, payload: ItemUpdate) -> ItemRead:
    item = items_service.update_item(item_id, payload)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@router.delete("/{item_id}", status_code=204)
def delete_item(item_id: int) -> None:
    deleted = items_service.delete_item(item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Item not found")
