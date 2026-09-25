from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class VisitorSessions(Base):
    __tablename__ = "visitor_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    visitor_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)
    country_code: Mapped[str] = mapped_column(String(8), default="", index=True, nullable=False)
    country_name: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    region: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    city: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    locale: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    device: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    browser: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    user_agent: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    ip_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    landing_path: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    current_path: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    referrer: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    screen_w: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    screen_h: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    click_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class VisitorEvents(Base):
    __tablename__ = "visitor_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    visitor_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    path: Mapped[str] = mapped_column(String(400), default="", index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    label: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    href: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    element: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    x_pct: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    y_pct: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
