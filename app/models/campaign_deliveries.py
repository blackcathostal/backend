from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

DELIVERY_STATUS_SENT = "sent"
DELIVERY_STATUS_OPENED = "opened"
DELIVERY_STATUS_BOUNCED = "bounced"
DELIVERY_STATUS_ERROR = "error"


class CampaignDeliveries(Base):
    __tablename__ = "campaign_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(220), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default=DELIVERY_STATUS_SENT, index=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tracking_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=func.now(),
    )
