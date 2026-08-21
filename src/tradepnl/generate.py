"""Synthetic trade generator.

The important thing about this generator is that it produces *bad* data on purpose.

Clean synthetic data proves nothing, because handling clean data is trivial. A
pipeline is only interesting to the extent that it survives the things that actually
go wrong: a producer retrying and sending the same trade twice, a price field
arriving empty, a booking system emitting an instrument the reference data has never
heard of. Every defect below is one that occurs in real trade feeds.

The defect rate is low and the generator is seeded, so runs are reproducible and the
aggregate figures stay realistic.
"""

from __future__ import annotations

import csv
import datetime as dt
import random
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

ASSET_CLASSES = ("EQUITY", "FX", "FUTURE")
CURRENCIES = ("USD", "EUR", "GBP", "JPY")

SYMBOLS = (
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "NVDA", "JPM",
    "BAC", "XOM", "CVX", "PFE", "JNJ", "WMT", "HD", "PG",
    "KO", "PEP", "DIS", "NFLX", "INTC", "AMD", "CRM", "ORCL",
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "ESZ5", "NQZ5",
)


@dataclass(frozen=True)
class InstrumentRow:
    """A row of the instrument dimension."""

    instrument_id: int
    symbol: str
    asset_class: str
    currency: str


@dataclass(frozen=True)
class TradeRow:
    """A row of the trade feed. Fields are strings because a CSV feed has no types."""

    trade_id: str
    instrument_id: str
    side: str
    quantity: str
    price: str
    executed_at: str


@dataclass(frozen=True)
class DefectProfile:
    """How many of each kind of bad record to inject.

    Counts rather than rates, so tests can assert on exact numbers.
    """

    duplicate_trade_ids: int = 40
    null_prices: int = 25
    non_positive_quantities: int = 20
    unknown_instruments: int = 15
    bad_sides: int = 10
    null_timestamps: int = 8

    @property
    def total(self) -> int:
        """Total number of defective rows this profile will introduce."""
        return (
            self.duplicate_trade_ids
            + self.null_prices
            + self.non_positive_quantities
            + self.unknown_instruments
            + self.bad_sides
            + self.null_timestamps
        )


def build_instruments() -> list[InstrumentRow]:
    """Build the instrument dimension. Deterministic: symbol order fixes the ids."""
    rows: list[InstrumentRow] = []
    for index, symbol in enumerate(SYMBOLS, start=1):
        if len(symbol) == 6 and symbol.isalpha():
            asset_class = "FX"
        elif symbol.endswith(("Z5", "H6")):
            asset_class = "FUTURE"
        else:
            asset_class = "EQUITY"
        currency = "USD" if asset_class != "FX" else symbol[3:]
        rows.append(InstrumentRow(index, symbol, asset_class, currency))
    return rows


def _base_price(rng: random.Random) -> Decimal:
    return Decimal(str(round(rng.uniform(15, 480), 2)))


def generate_trades(
    instruments: list[InstrumentRow],
    *,
    count: int = 50_000,
    start: dt.date | None = None,
    days: int = 365,
    seed: int = 20240101,
    defects: DefectProfile | None = None,
) -> list[TradeRow]:
    """Generate ``count`` clean trades, then inject the defects on top.

    The clean rows are generated first and the defects layered over them so the
    proportion of bad data stays predictable regardless of ``count``.
    """
    rng = random.Random(seed)
    defects = defects or DefectProfile()
    start = start or (dt.date.today() - dt.timedelta(days=days))

    # A per-instrument price that random-walks, so prices are correlated across a day
    # rather than being independent noise. Realised PnL is meaningless otherwise.
    price_by_instrument = {inst.instrument_id: _base_price(rng) for inst in instruments}

    rows: list[TradeRow] = []
    for sequence in range(count):
        inst = rng.choice(instruments)
        drift = Decimal(str(round(rng.gauss(0, 0.6), 4)))
        price = max(Decimal("0.5"), price_by_instrument[inst.instrument_id] + drift)
        price_by_instrument[inst.instrument_id] = price

        offset_days = rng.randrange(days)
        executed = dt.datetime.combine(
            start + dt.timedelta(days=offset_days),
            dt.time(hour=rng.randrange(8, 17), minute=rng.randrange(60), second=rng.randrange(60)),
            tzinfo=dt.timezone.utc,
        )

        rows.append(
            TradeRow(
                trade_id=f"T{sequence:08d}",
                instrument_id=str(inst.instrument_id),
                side=rng.choice(("BUY", "SELL")),
                quantity=str(rng.randrange(1, 500)),
                price=f"{price:.6f}",
                executed_at=executed.isoformat(),
            )
        )

    _inject_defects(rows, rng, defects, len(instruments))
    rng.shuffle(rows)
    return rows


def _inject_defects(
    rows: list[TradeRow],
    rng: random.Random,
    defects: DefectProfile,
    instrument_count: int,
) -> None:
    """Corrupt a sample of rows in place, and append duplicates."""
    if not rows:
        return

    def sample_indices(n: int, taken: set[int]) -> list[int]:
        chosen: list[int] = []
        while len(chosen) < n:
            index = rng.randrange(len(rows))
            if index not in taken:
                taken.add(index)
                chosen.append(index)
        return chosen

    taken: set[int] = set()

    for index in sample_indices(defects.null_prices, taken):
        rows[index] = TradeRow(**{**asdict(rows[index]), "price": ""})

    for index in sample_indices(defects.non_positive_quantities, taken):
        quantity = "0" if rng.random() < 0.5 else str(-rng.randrange(1, 100))
        rows[index] = TradeRow(**{**asdict(rows[index]), "quantity": quantity})

    for index in sample_indices(defects.unknown_instruments, taken):
        orphan = str(instrument_count + rng.randrange(100, 999))
        rows[index] = TradeRow(**{**asdict(rows[index]), "instrument_id": orphan})

    for index in sample_indices(defects.bad_sides, taken):
        rows[index] = TradeRow(**{**asdict(rows[index]), "side": rng.choice(("B", "S", ""))})

    for index in sample_indices(defects.null_timestamps, taken):
        rows[index] = TradeRow(**{**asdict(rows[index]), "executed_at": ""})

    # Duplicates are appended rather than edited: a retrying producer sends the same
    # trade_id again, usually with a slightly different price after a re-fill.
    for index in sample_indices(defects.duplicate_trade_ids, taken):
        original = rows[index]
        nudged = Decimal(original.price or "1") + Decimal("0.01")
        rows.append(TradeRow(**{**asdict(original), "price": f"{nudged:.6f}"}))


TRADE_COLUMNS = ("trade_id", "instrument_id", "side", "quantity", "price", "executed_at")


def write_trades_csv(rows: list[TradeRow], path: Path) -> Path:
    """Write the feed to CSV, which is how a real batch feed usually arrives."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRADE_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))
    return path


def read_trades_csv(path: Path) -> list[TradeRow]:
    """Read a feed file back."""
    with path.open(newline="", encoding="utf-8") as handle:
        return [TradeRow(**row) for row in csv.DictReader(handle)]
