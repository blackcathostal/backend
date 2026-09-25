from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Complaints(Base):
    __tablename__ = "complaints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tracking_code: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)
    complaint_type: Mapped[str] = mapped_column(String(60), nullable=False)
    complaint_type_other: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    relation: Mapped[str] = mapped_column(String(60), nullable=False)
    relation_other: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    location: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    happened_at: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    accused_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    document_type: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    document_number: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    data_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    data_consent_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="received")
    investigator_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    measures: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attachments: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    events: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=func.now(),
    )
