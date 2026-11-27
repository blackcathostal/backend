"""Cached FNS occupancy snapshots (daily totals + per room type)."""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OccupancyDayStats(Base):
    """One row per calendar day: rooms/guests activity + total occupancy %."""

    __tablename__ = "occupancy_day_stats"
    __table_args__ = (UniqueConstraint("day_date", name="uq_occupancy_day_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)  # YYYY-MM
    day_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    weekday: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    rooms_occupied: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    guests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hosts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    check_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    check_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    occupancy_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_rooms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class OccupancyRoomTypeStats(Base):
    """Occupancy % for one room type on one calendar day."""

    __tablename__ = "occupancy_room_type_stats"
    __table_args__ = (
        UniqueConstraint(
            "day_date",
            "room_type",
            name="uq_occupancy_room_type_day",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    day_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    room_type: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occupancy_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
