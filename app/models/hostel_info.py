from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class HostelInfo(Base):
    """Singleton row (id=1) with operational facts for DeepSeek Instagram replies."""

    __tablename__ = "hostel_info"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    check_in_time: Mapped[str] = mapped_column(String(40), nullable=False, default="15:00")
    check_out_time: Mapped[str] = mapped_column(String(40), nullable=False, default="12:00")
    breakfast_hours: Mapped[str] = mapped_column(String(80), nullable=False, default="08:00-10:00")
    has_parking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parking_notes: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="No on-site parking; paid nearby options available in Barrio Brasil.",
    )
    whatsapp_url: Mapped[str] = mapped_column(
        String(255), nullable=False, default="https://wa.me/56949105984"
    )
    address: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="Compania de Jesus 1921, Barrio Brasil, Santiago, Chile",
    )
    email: Mapped[str] = mapped_column(String(160), nullable=False, default="reservas@blackcathostal.com")
    website: Mapped[str] = mapped_column(String(255), nullable=False, default="https://blackcathostal.com")
    extra_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class CommonAnswers(Base):
    """Guided answers for frequent Instagram / guest questions."""

    __tablename__ = "common_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    topic: Mapped[str] = mapped_column(String(80), nullable=False, default="general")
    keywords: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    question: Mapped[str] = mapped_column(String(255), nullable=False)
    answer_guide: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
