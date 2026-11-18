from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Audits(Base):
    """Admin activity audit: navigation, clicks, screen duration."""

    __tablename__ = "audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, default="navigation", index=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    path: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    screen: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
