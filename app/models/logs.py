from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Logs(Base):
    """Frontend and backend error/event logs."""

    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="backend", index=True)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="error", index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stack: Mapped[str] = mapped_column(Text, nullable=False, default="")
    path: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    meta_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
