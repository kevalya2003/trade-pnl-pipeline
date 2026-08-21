"""Test fixtures.

These tests run against a real PostgreSQL instance rather than SQLite or mocks. That
is a deliberate choice: the pipeline depends on ON CONFLICT, FILTER, DISTINCT ON and
window frames, none of which behave identically elsewhere. A test suite that passes
against a database you do not deploy to is testing the wrong thing.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text

from tradepnl.db import create_schema, drop_schema, make_engine
from tradepnl.generate import TradeRow

TEST_URL = os.environ.get(
    "TRADEPNL_TEST_DATABASE_URL",
    os.environ.get(
        "TRADEPNL_DATABASE_URL",
        "postgresql+psycopg://trades:trades@localhost:55432/trades",
    ),
)

# CI sets this so a missing database fails the build instead of quietly skipping,
# which would turn a broken pipeline into a green tick.
REQUIRE_DB = os.environ.get("TRADEPNL_REQUIRE_DB") == "1"


@pytest.fixture(scope="session")
def engine() -> Engine:
    """Session-wide engine with the schema created once."""
    eng = make_engine(TEST_URL)
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        message = f"PostgreSQL not reachable at {TEST_URL}: {exc}"
        if REQUIRE_DB:
            pytest.fail(message)
        pytest.skip(f"{message}\nStart one with: docker compose up -d postgres")

    drop_schema(eng)
    create_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def clean_db(engine: Engine) -> Engine:
    """Empty every table before each test so tests cannot leak into one another."""
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE trade, daily_pnl, etl_watermark, instrument CASCADE"))
    return engine


@pytest.fixture()
def seeded_instruments(clean_db: Engine) -> Engine:
    """Three instruments, enough for referential integrity to mean something."""
    with clean_db.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO instrument (instrument_id, symbol, asset_class, currency)
                VALUES (1, 'AAPL', 'EQUITY', 'USD'),
                       (2, 'MSFT', 'EQUITY', 'USD'),
                       (3, 'EURUSD', 'FX', 'USD')
                """
            )
        )
    return clean_db


def at(day: int, hour: int = 10) -> str:
    """A timestamp on day N of January 2024.

    Scenarios read as "buy on day one, sell on day two", which is what they are about;
    spelling out full ISO timestamps inline buries that under punctuation.
    """
    return dt.datetime(2024, 1, day, hour, tzinfo=dt.timezone.utc).isoformat()


def trade(
    trade_id: str,
    *,
    instrument_id: int | str = 1,
    side: str = "BUY",
    quantity: str | int = 100,
    price: str | Decimal = "10.00",
    executed_at: str | dt.datetime = "2024-01-01T10:00:00+00:00",
) -> TradeRow:
    """Terse builder so the tests read as scenarios rather than as data setup."""
    if isinstance(executed_at, dt.datetime):
        executed_at = executed_at.isoformat()
    return TradeRow(
        trade_id=trade_id,
        instrument_id=str(instrument_id),
        side=side,
        quantity=str(quantity),
        price=str(price),
        executed_at=executed_at,
    )


def count_rows(engine: Engine, table: str) -> int:
    """Row count for a table or view."""
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
