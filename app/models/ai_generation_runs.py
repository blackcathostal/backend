from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AiGenerationRuns(Base):
    __tablename__ = "ai_generation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    operation: Mapped[str] = mapped_column(String(80), nullable=False, default="refresh_content")
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="deepseek")
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_ids: Mapped[str] = mapped_column(Text, nullable=False, default="")
    generated_title: Mapped[str | None] = mapped_column(String(220), nullable=True)
    generated_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_keywords: Mapped[str | None] = mapped_column(String(500), nullable=True)
    generated_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
