"""Idempotent, incremental loader for the trade feed.

Two properties matter here and both are things an interviewer will probe.

Idempotency. Running the loader twice over the same feed must leave the database in
the state it would have been in after running it once. This is not a nicety: batch
jobs get retried, they die halfway through, and someone eventually runs the same file
twice by hand. The mechanism is PostgreSQL's INSERT ... ON CONFLICT (trade_id) DO
UPDATE, which makes the insert converge rather than accumulate.

Incrementality. Reloading a year of history to pick up yesterday's trades gets slower
every day. A watermark -- the highest executed_at successfully loaded -- lets a run
consider only what is new. The watermark is written in the same transaction as the
rows it describes, so it cannot drift out of step with them: if the load rolls back,
so does the watermark.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from tradepnl.generate import TradeRow
from tradepnl.models import EtlWatermark, Trade

DEFAULT_STREAM = "trades_csv"
DEFAULT_BATCH_SIZE = 2_000


@dataclass(frozen=True)
class LoadResult:
    """What a single load run did. Returned rather than logged so tests can assert."""

    rows_read: int
    rows_upserted: int
    rows_skipped_by_watermark: int
    duplicates_collapsed: int
    watermark_before: dt.datetime | None
    watermark_after: dt.datetime | None

    def summary(self) -> str:
        """One-line human summary for the CLI."""
        return (
            f"read {self.rows_read}, upserted {self.rows_upserted}, "
            f"skipped {self.rows_skipped_by_watermark} below watermark, "
            f"collapsed {self.duplicates_collapsed} in-batch duplicates"
        )


def _to_decimal(raw: str | None) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def _to_int(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _to_datetime(raw: str | None) -> dt.datetime | None:
    if raw is None or raw == "":
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def to_record(row: TradeRow, source_file: str | None) -> dict[str, Any]:
    """Coerce one feed row into column values.

    A value that will not parse becomes NULL rather than raising. That is deliberate:
    the loader's job is to land the feed, not to judge it. A row with an unparseable
    price still needs to reach the database so the data quality stage can report on it
    and quarantine it. Rejecting it here would make the bad row invisible.
    """
    return {
        "trade_id": row.trade_id,
        "instrument_id": _to_int(row.instrument_id),
        "side": (row.side or None),
        "quantity": _to_decimal(row.quantity),
        "price": _to_decimal(row.price),
        "executed_at": _to_datetime(row.executed_at),
        "source_file": source_file,
    }


def read_watermark(engine: Engine, stream: str = DEFAULT_STREAM) -> dt.datetime | None:
    """Return the current high-water mark for a stream, or None if never loaded."""
    with engine.connect() as conn:
        return conn.execute(
            select(EtlWatermark.watermark_at).where(EtlWatermark.stream == stream)
        ).scalar_one_or_none()


def _collapse_duplicates(records: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Reduce a batch to one record per trade_id, keeping the last occurrence.

    This is not optional. PostgreSQL raises "ON CONFLICT DO UPDATE command cannot
    affect row a second time" if a single INSERT presents the same conflict target
    twice, and a feed containing a producer's retry will do exactly that. Last write
    wins, which matches the semantics of a re-fill correcting an earlier message.
    """
    deduplicated: dict[str, dict[str, Any]] = {}
    for record in records:
        deduplicated[record["trade_id"]] = record
    return list(deduplicated.values()), len(records) - len(deduplicated)


def load_trades(
    engine: Engine,
    rows: Iterable[TradeRow],
    *,
    stream: str = DEFAULT_STREAM,
    source_file: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    incremental: bool = True,
) -> LoadResult:
    """Upsert a feed into ``trade`` and advance the watermark.

    The whole run is one transaction. A partial load that fails leaves no rows and no
    watermark movement, which is what makes a blind retry safe.
    """
    # The stored watermark has two distinct jobs: it filters the input on an
    # incremental run, and it is the floor below which the mark must never fall. Only
    # the first job is skipped by --full. Conflating the two lets a full backfill of
    # old trades rewind the mark, which would silently reprocess everything after it.
    stored_watermark = read_watermark(engine, stream)
    filter_watermark = stored_watermark if incremental else None

    rows_read = 0
    skipped = 0
    candidates: list[dict[str, Any]] = []

    for row in rows:
        rows_read += 1
        record = to_record(row, source_file)
        executed_at = record["executed_at"]
        # A row with no timestamp cannot be positioned against the watermark, so it is
        # always let through. The upsert makes re-presenting it harmless.
        if filter_watermark is not None and executed_at is not None:
            if executed_at <= filter_watermark:
                skipped += 1
                continue
        candidates.append(record)

    records, collapsed = _collapse_duplicates(candidates)

    upserted = 0
    max_executed: dt.datetime | None = None

    with engine.begin() as conn:
        for start in range(0, len(records), batch_size):
            batch = records[start : start + batch_size]
            statement = pg_insert(Trade).values(batch)
            statement = statement.on_conflict_do_update(
                index_elements=[Trade.trade_id],
                set_={
                    "instrument_id": statement.excluded.instrument_id,
                    "side": statement.excluded.side,
                    "quantity": statement.excluded.quantity,
                    "price": statement.excluded.price,
                    "executed_at": statement.excluded.executed_at,
                    "source_file": statement.excluded.source_file,
                },
            )
            conn.execute(statement)
            upserted += len(batch)

            for record in batch:
                executed_at = record["executed_at"]
                if executed_at is not None and (max_executed is None or executed_at > max_executed):
                    max_executed = executed_at

        watermark_after = _advance_watermark(
            conn, stream, stored_watermark, max_executed, upserted
        )

    return LoadResult(
        rows_read=rows_read,
        rows_upserted=upserted,
        rows_skipped_by_watermark=skipped,
        duplicates_collapsed=collapsed,
        watermark_before=stored_watermark,
        watermark_after=watermark_after,
    )


def _advance_watermark(
    conn: Any,
    stream: str,
    before: dt.datetime | None,
    max_executed: dt.datetime | None,
    rows_loaded: int,
) -> dt.datetime | None:
    """Move the watermark forward, never backward.

    Monotonicity matters: a late-arriving backfill of old trades must not rewind the
    mark and cause the next run to reprocess everything after it.
    """
    target = max_executed
    if before is not None and (target is None or target < before):
        target = before

    statement = pg_insert(EtlWatermark).values(
        stream=stream,
        watermark_at=target,
        rows_loaded=rows_loaded,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[EtlWatermark.stream],
        set_={
            "watermark_at": statement.excluded.watermark_at,
            "rows_loaded": EtlWatermark.rows_loaded + statement.excluded.rows_loaded,
            "updated_at": dt.datetime.now(dt.timezone.utc),
        },
    )
    conn.execute(statement)
    return target
