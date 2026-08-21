"""PnL aggregation and the views built on top of it.

The figures below are worked out by hand in the docstrings. Asserting against a
number the code itself produced would test nothing.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Engine, text

from tests.conftest import at, count_rows, trade
from tradepnl.load import load_trades
from tradepnl.pnl import compute_daily_pnl


def _rows(engine: Engine, sql: str) -> list:
    with engine.connect() as conn:
        return conn.execute(text(sql)).mappings().all()


def test_realised_pnl_uses_average_cost_over_buys(seeded_instruments: Engine) -> None:
    """Buy 100 at 10 and 100 at 20, so average cost is 15. Sell 50 at 25.

    Realised PnL on the sell is (25 - 15) * 50 = 500.
    Closing position after the sell is 200 - 50 = 150.
    Unrealised at a mark of 25 is (25 - 15) * 150 = 1500.
    """
    load_trades(
        seeded_instruments,
        [
            trade("B1", side="BUY", quantity=100, price="10", executed_at=at(1, 10)),
            trade("B2", side="BUY", quantity=100, price="20", executed_at=at(1, 11)),
            trade("S1", side="SELL", quantity=50, price="25", executed_at=at(2)),
        ],
        incremental=False,
    )

    compute_daily_pnl(seeded_instruments)

    rows = _rows(
        seeded_instruments,
        "SELECT * FROM daily_pnl WHERE instrument_id = 1 ORDER BY pnl_date",
    )

    day_one, day_two = rows
    assert day_one["trade_count"] == 2
    assert day_one["closing_position"] == Decimal("200.0000")
    assert day_one["avg_cost"] == Decimal("15.000000")
    assert day_one["realised_pnl"] == Decimal("0.000000")
    # Marked at the last trade of the day, which was the buy at 20.
    assert day_one["unrealised_pnl"] == Decimal("1000.000000")

    assert day_two["realised_pnl"] == Decimal("500.000000")
    assert day_two["closing_position"] == Decimal("150.0000")
    assert day_two["unrealised_pnl"] == Decimal("1500.000000")


def test_recomputing_pnl_is_idempotent(seeded_instruments: Engine) -> None:
    """The aggregation upserts, so running it twice must converge, not accumulate."""
    load_trades(
        seeded_instruments,
        [
            trade("B1", side="BUY", quantity=10, price="5"),
            trade("S1", side="SELL", quantity=4, price="9", executed_at=at(2)),
        ],
        incremental=False,
    )

    compute_daily_pnl(seeded_instruments)
    first = _rows(seeded_instruments, "SELECT pnl_date, realised_pnl FROM daily_pnl ORDER BY 1")

    compute_daily_pnl(seeded_instruments)
    second = _rows(seeded_instruments, "SELECT pnl_date, realised_pnl FROM daily_pnl ORDER BY 1")

    assert first == second
    assert count_rows(seeded_instruments, "daily_pnl") == 2


def test_invalid_rows_are_excluded_from_the_aggregate(seeded_instruments: Engine) -> None:
    """Every defect the generator injects must fall out of v_valid_trade."""
    load_trades(
        seeded_instruments,
        [
            trade("GOOD", side="BUY", quantity=10, price="5"),
            trade("NULL_PRICE", price=""),
            trade("ZERO_QTY", quantity=0),
            trade("NEG_QTY", quantity=-5),
            trade("ORPHAN", instrument_id=999),
            trade("BAD_SIDE", side="B"),
            trade("NO_TIME", executed_at=""),
        ],
        incremental=False,
    )

    assert count_rows(seeded_instruments, "trade") == 7
    assert count_rows(seeded_instruments, "v_valid_trade") == 1

    compute_daily_pnl(seeded_instruments)
    assert count_rows(seeded_instruments, "daily_pnl") == 1


def test_running_pnl_accumulates_realised_but_not_unrealised(seeded_instruments: Engine) -> None:
    """Unrealised is a position marked at a point in time, not a flow.

    Summing it across days would count the same open position repeatedly, so the view
    accumulates realised only and adds the current day's unrealised on top.
    """
    load_trades(
        seeded_instruments,
        [
            trade("B1", side="BUY", quantity=100, price="10", executed_at=at(1)),
            trade("S1", side="SELL", quantity=10, price="12", executed_at=at(2)),
            trade("S2", side="SELL", quantity=10, price="14", executed_at=at(3)),
        ],
        incremental=False,
    )
    compute_daily_pnl(seeded_instruments)

    rows = _rows(
        seeded_instruments,
        """
        SELECT pnl_date, realised_pnl, cumulative_realised_pnl, unrealised_pnl, total_pnl
        FROM v_running_pnl WHERE instrument_id = 1 ORDER BY pnl_date
        """,
    )

    # (12 - 10) * 10 = 20 on day two, (14 - 10) * 10 = 40 on day three.
    assert [r["realised_pnl"] for r in rows] == [
        Decimal("0.000000"),
        Decimal("20.000000"),
        Decimal("40.000000"),
    ]
    assert [r["cumulative_realised_pnl"] for r in rows] == [
        Decimal("0.000000"),
        Decimal("20.000000"),
        Decimal("60.000000"),
    ]
    for row in rows:
        assert row["total_pnl"] == row["cumulative_realised_pnl"] + row["unrealised_pnl"]


def test_monthly_ranking_is_dense_over_ties(seeded_instruments: Engine) -> None:
    """RANK rather than ROW_NUMBER, so equal PnL shares a rank."""
    load_trades(
        seeded_instruments,
        [
            trade("A1", instrument_id=1, side="BUY", quantity=10, price="10", executed_at=at(1)),
            trade("A2", instrument_id=1, side="SELL", quantity=10, price="20", executed_at=at(5)),
            trade("B1", instrument_id=2, side="BUY", quantity=10, price="10", executed_at=at(1)),
            trade("B2", instrument_id=2, side="SELL", quantity=10, price="20", executed_at=at(5)),
        ],
        incremental=False,
    )
    compute_daily_pnl(seeded_instruments)

    rows = _rows(
        seeded_instruments,
        "SELECT symbol, realised_pnl, pnl_rank FROM v_top_instruments_by_month ORDER BY symbol",
    )

    assert {r["symbol"] for r in rows} == {"AAPL", "MSFT"}
    assert all(r["realised_pnl"] == Decimal("100.000000") for r in rows)
    assert {r["pnl_rank"] for r in rows} == {1}


def test_since_limits_the_recomputed_window(seeded_instruments: Engine) -> None:
    """A daily run should not have to rebuild a year of history."""
    import datetime as dt

    load_trades(
        seeded_instruments,
        [
            trade("OLD", executed_at="2024-01-01T10:00:00+00:00"),
            trade("NEW", executed_at="2024-06-01T10:00:00+00:00"),
        ],
        incremental=False,
    )

    compute_daily_pnl(seeded_instruments, since=dt.date(2024, 5, 1))

    dates = [r["pnl_date"] for r in _rows(seeded_instruments, "SELECT pnl_date FROM daily_pnl")]
    assert dates == [dt.date(2024, 6, 1)]
