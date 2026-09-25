from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


COMPLAINT_TYPES = {
    "abuso_poder": "Abuso de poder",
    "acoso_laboral": "Acoso laboral",
    "acoso_sexual": "Acoso sexual",
    "violencia_trabajo": "Violencia en el trabajo",
    "otro": "Otro",
}

RELATIONS = {
    "candidato": "Candidato / Postulante",
    "cliente": "Cliente",
    "colaborador": "Colaborador",
    "contratista": "Contratista",
    "proveedor": "Proveedor",
    "otro": "Otro",
}

STATUSES = {
    "received": "Recibida",
    "under_review": "En revisión (3 días hábiles)",
    "internal_investigation": "Investigación interna",
    "referred_dt": "Derivada a Dirección del Trabajo",
    "closed": "Cerrada",
}


class ComplaintOut(BaseModel):
    id: int
    tracking_code: str
    complaint_type: str
    complaint_type_label: str = ""
    complaint_type_other: str = ""
    relation: str
    relation_label: str = ""
    relation_other: str = ""
    location: str = ""
    happened_at: str = ""
    description: str
    accused_name: str
    is_anonymous: bool
    full_name: str = ""
    email: str = ""
    phone: str = ""
    document_type: str = ""
    document_number: str = ""
    data_consent: bool = False
    data_consent_accepted_at: datetime | None = None
    status: str
    status_label: str = ""
    investigator_name: str = ""
    measures: str = ""
    attachments: list[Any] = Field(default_factory=list)
    events: list[Any] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    whatsapp_url: str = ""

    class Config:
        from_attributes = True


class ComplaintPublicOut(BaseModel):
    tracking_code: str
    status: str
    status_label: str
    created_at: datetime | None = None
    last_message: str = ""
    investigator_name: str = ""


class ComplaintSubmitOut(BaseModel):
    ok: bool = True
    tracking_code: str
    message: str
    status_label: str


class ComplaintUpdate(BaseModel):
    status: str | None = None
    note: str = Field(default="", max_length=4000)
    investigator_name: str | None = Field(default=None, max_length=160)
    measures: str | None = Field(default=None, max_length=4000)
    notify_email: bool = True
    notify_whatsapp: bool = True
