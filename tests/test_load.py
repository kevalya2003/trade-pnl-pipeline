"""Loader behaviour.

The first test in this file is the one that matters. Idempotency is the property an
interviewer will ask about, and being able to point at a test that demonstrates it is
what separates having built this from having read about it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import Engine, text

from tests.conftest import count_rows, trade
from tradepnl.load import load_trades, read_watermark


def test_loading_the_same_feed_twice_does_not_duplicate(seeded_instruments: Engine) -> None:
    feed = [trade("T1"), trade("T2"), trade("T3")]

    load_trades(seeded_instruments, feed, incremental=False)
    after_first = count_rows(seeded_instruments, "trade")

    load_trades(seeded_instruments, feed, incremental=False)
    after_second = count_rows(seeded_instruments, "trade")

    assert after_first == 3
    assert after_second == after_first


def test_reloading_a_corrected_trade_updates_it_in_place(seeded_instruments: Engine) -> None:
    load_trades(seeded_instruments, [trade("T1", price="10.00")], incremental=False)
    load_trades(seeded_instruments, [trade("T1", price="12.50")], incremental=False)

    with seeded_instruments.connect() as conn:
        price = conn.execute(text("SELECT price FROM trade WHERE trade_id = 'T1'")).scalar_one()

    assert price == Decimal("12.500000")
    assert count_rows(seeded_instruments, "trade") == 1


def test_duplicate_trade_ids_within_one_batch_are_collapsed(seeded_instruments: Engine) -> None:
    """PostgreSQL refuses to let one INSERT hit the same conflict target twice.

    A feed containing a producer's retry does exactly that, so the loader has to
    reduce the batch first. Last occurrence wins.
    """
    feed = [trade("T1", price="10.00"), trade("T1", price="11.00"), trade("T2")]

    result = load_trades(seeded_instruments, feed, incremental=False)

    assert result.duplicates_collapsed == 1
    assert count_rows(seeded_instruments, "trade") == 2
    with seeded_instruments.connect() as conn:
        price = conn.execute(text("SELECT price FROM trade WHERE trade_id = 'T1'")).scalar_one()
    assert price == Decimal("11.000000")


def test_unparseable_values_land_as_null_rather_than_failing(seeded_instruments: Engine) -> None:
    """A bad row must reach the database, otherwise it cannot be reported on."""
    feed = [trade("T1", price="", quantity="not-a-number", executed_at="")]

    load_trades(seeded_instruments, feed, incremental=False)

    with seeded_instruments.connect() as conn:
        row = conn.execute(
            text("SELECT price, quantity, executed_at FROM trade WHERE trade_id = 'T1'")
        ).one()

    assert row.price is None
    assert row.quantity is None
    assert row.executed_at is None
    assert count_rows(seeded_instruments, "trade") == 1


def test_watermark_advances_to_the_latest_executed_at(seeded_instruments: Engine) -> None:
    feed = [
        trade("T1", executed_at="2024-01-01T10:00:00+00:00"),
        trade("T2", executed_at="2024-03-05T16:30:00+00:00"),
        trade("T3", executed_at="2024-02-01T09:00:00+00:00"),
    ]

    result = load_trades(seeded_instruments, feed)

    assert result.watermark_before is None
    assert result.watermark_after == dt.datetime(2024, 3, 5, 16, 30, tzinfo=dt.timezone.utc)
    assert read_watermark(seeded_instruments) == result.watermark_after


def test_incremental_run_skips_rows_at_or_below_the_watermark(seeded_instruments: Engine) -> None:
    load_trades(seeded_instruments, [trade("T1", executed_at="2024-02-01T09:00:00+00:00")])

    second = load_trades(
        seeded_instruments,
        [
            trade("T1", executed_at="2024-02-01T09:00:00+00:00"),
            trade("T2", executed_at="2024-01-01T09:00:00+00:00"),
            trade("T3", executed_at="2024-03-01T09:00:00+00:00"),
        ],
    )

    assert second.rows_skipped_by_watermark == 2
    assert second.rows_upserted == 1
    assert count_rows(seeded_instruments, "trade") == 2


def test_watermark_never_moves_backwards(seeded_instruments: Engine) -> None:
    """A backfill of old trades must not rewind the mark and cause a full reprocess."""
    load_trades(seeded_instruments, [trade("T1", executed_at="2024-06-01T09:00:00+00:00")])

    backfill = load_trades(
        seeded_instruments,
        [trade("T2", executed_at="2024-01-01T09:00:00+00:00")],
        incremental=False,
    )

    assert backfill.watermark_after == dt.datetime(2024, 6, 1, 9, 0, tzinfo=dt.timezone.utc)


def test_a_full_run_reconsiders_rows_the_watermark_would_have_skipped(
    seeded_instruments: Engine,
) -> None:
    load_trades(seeded_instruments, [trade("T1", executed_at="2024-06-01T09:00:00+00:00")])

    full = load_trades(
        seeded_instruments,
        [trade("T2", executed_at="2024-01-01T09:00:00+00:00")],
        incremental=False,
    )

    assert full.rows_skipped_by_watermark == 0
    assert count_rows(seeded_instruments, "trade") == 2
