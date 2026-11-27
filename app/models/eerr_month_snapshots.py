"""Cached monthly EERR payloads for fast consolidated reports (no FNS)."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EerrMonthSnapshots(Base):
    """One row per YYYY-MM: full monthly EERR JSON after GET /reports/eerr."""

    __tablename__ = "eerr_month_snapshots"
    __table_args__ = (UniqueConstraint("period", name="uq_eerr_month_snapshot_period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)  # YYYY-MM
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    source: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
