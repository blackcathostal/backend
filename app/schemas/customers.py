from datetime import datetime

from pydantic import BaseModel


class CustomerOut(BaseModel):
    id: int
    identification_number: str
    company_name: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
