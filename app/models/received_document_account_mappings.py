from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ReceivedDocumentAccountMappings(Base):
    """Maps received-document suppliers (RUT) to accounting accounts."""

    __tablename__ = "received_document_account_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    identification_number: Mapped[str] = mapped_column(
        String(80), unique=True, nullable=False, index=True
    )
    company_name: Mapped[str] = mapped_column(String(180), nullable=False, default="")
    accounting_account_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("accounting_accounts.id"),
        nullable=False,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    accounting_account = relationship("AccountingAccounts")
