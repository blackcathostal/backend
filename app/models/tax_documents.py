from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

DTE_TYPE_ISSUED = 1
DTE_TYPE_RECEIVED = 2
DTE_STATUS_PENDING_PAYMENT = 1
DTE_STATUS_PAID = 2


class DteTypes(Base):
    __tablename__ = "dte_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, default="")


class DteStatuses(Base):
    """Payment/imputation statuses for issued and received tax documents."""

    __tablename__ = "dte_statuses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    tip_color: Mapped[str] = mapped_column(String(20), nullable=False, default="red")


class TaxDocuments(Base):
    """Tax document (documento tributario): boleta/factura issued or received."""

    __tablename__ = "tax_documents"
    __table_args__ = (
        UniqueConstraint(
            "direction",
            "doc_type",
            "folio",
            "counterpart_rut",
            name="uq_tax_documents_doc",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    direction: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    dte_type_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("dte_types.id"),
        nullable=True,
        index=True,
    )
    status_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("dte_statuses.id"),
        nullable=True,
        index=True,
    )
    period: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    doc_type: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    folio: Mapped[str] = mapped_column(String(40), nullable=False, default="", index=True)
    counterpart_rut: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    counterpart_name: Mapped[str] = mapped_column(String(220), nullable=False, default="")
    issued_at: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    net_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    exempt_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tax_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    branch: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    pms_class: Mapped[str] = mapped_column(String(80), nullable=False, default="Sin match", index=True)
    pms_ref: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="api")
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    dte_type = relationship("DteTypes")
    status = relationship("DteStatuses")
