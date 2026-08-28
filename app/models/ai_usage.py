from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AiUsage(Base):
    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="deepseek")
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    operation: Mapped[str] = mapped_column(String(80), nullable=False, default="refresh_content")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="success")
    api_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_cache_hit_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_cache_miss_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_hit_price_per_million: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cache_miss_price_per_million: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    output_price_per_million: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    platform_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    platform_balance_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
