"""Database schema.

Two decisions here are worth understanding before changing anything.

Grain. ``trade`` holds one row per execution and ``daily_pnl`` holds one row per
instrument per day. ``daily_pnl`` is a table rather than a view because position
history is read far more often than it changes, and recomputing it from every
execution gets linearly slower as trades accumulate.

Constraints. ``trade`` is deliberately permissive: ``instrument_id`` has no foreign
key and most columns are nullable. That looks wrong until you consider what happens
when a bad row arrives. A foreign key would abort the whole batch for one orphaned
trade. Instead the constraints that would reject a row are expressed as data quality
checks downstream, which lets a bad row be quarantined while the rest of the batch
loads. ``trade_id`` is the exception: it is the primary key because the loader needs
a conflict target to be idempotent.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base carrying a stable constraint naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Instrument(Base):
    """The tradable universe. A conventional dimension: small, slowly changing."""

    __tablename__ = "instrument"

    instrument_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)


class Trade(Base):
    """One row per execution, as received from the source.

    Nullable columns are intentional. This table is the landing zone, so it has to be
    able to hold a row that is wrong in order for the pipeline to report that the row
    is wrong. Correctness is asserted downstream, not at insert time.
    """

    __tablename__ = "trade"

    trade_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    instrument_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    executed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ingested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_trade_executed_at", "executed_at"),
        Index("ix_trade_instrument_executed", "instrument_id", "executed_at"),
    )


class DailyPnl(Base):
    """Aggregate at one row per instrument per day.

    ``realised_pnl`` uses average-cost basis on buys. See ``sql/daily_pnl.sql`` for
    why that method was chosen over FIFO.
    """

    __tablename__ = "daily_pnl"

    pnl_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    closing_position: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    avg_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    close_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    realised_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    unrealised_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    computed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EtlWatermark(Base):
    """High-water mark per ingestion stream, so loads can be incremental.

    Storing this in the database rather than a file means the watermark commits in the
    same transaction as the rows it describes. If the load rolls back, so does the
    watermark, and the two can never disagree.
    """

    __tablename__ = "etl_watermark"

    stream: Mapped[str] = mapped_column(String(64), primary_key=True)
    watermark_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rows_loaded: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
