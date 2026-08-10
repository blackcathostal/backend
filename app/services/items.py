from app.schemas.item import ItemCreate, ItemRead, ItemUpdate

_items: list[ItemRead] = [
    ItemRead(id=1, name="Café BlackCat", description="Espresso artesanal", price=3.5),
    ItemRead(id=2, name="Latte Nocturno", description="Con leche y cacao", price=4.25),
]
_next_id = 3


def list_items() -> list[ItemRead]:
    return list(_items)


def get_item(item_id: int) -> ItemRead | None:
    return next((item for item in _items if item.id == item_id), None)


def create_item(payload: ItemCreate) -> ItemRead:
    global _next_id
    item = ItemRead(id=_next_id, **payload.model_dump())
    _next_id += 1
    _items.append(item)
    return item


def update_item(item_id: int, payload: ItemUpdate) -> ItemRead | None:
    item = get_item(item_id)
    if item is None:
        return None

    data = item.model_dump()
    data.update(payload.model_dump(exclude_unset=True))
    updated = ItemRead(**data)

    for index, current in enumerate(_items):
        if current.id == item_id:
            _items[index] = updated
            break

    return updated


def delete_item(item_id: int) -> bool:
    global _items
    before = len(_items)
    _items = [item for item in _items if item.id != item_id]
    return len(_items) < before
