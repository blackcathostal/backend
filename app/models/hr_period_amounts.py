from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class HrPeriodAmounts(Base):
    """Monthly integer amounts for Remuneraciones (human resources) accounts."""

    __tablename__ = "hr_period_amounts"
    __table_args__ = (
        UniqueConstraint(
            "period",
            "accounting_account_id",
            name="uq_hr_period_account",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    period: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    accounting_account_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("accounting_accounts.id"),
        nullable=False,
        index=True,
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    accounting_account = relationship("AccountingAccounts")
