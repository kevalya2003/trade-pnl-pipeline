"""Daily PnL aggregation.

The work is done by ``sql/daily_pnl.sql``. This module exists to run it and report
what it did, not to reimplement it in Python.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import Engine, text

from tradepnl.db import read_sql


@dataclass(frozen=True)
class PnlResult:
    """Outcome of an aggregation run."""

    rows_written: int
    since: dt.date | None

    def summary(self) -> str:
        """One-line human summary for the CLI."""
        scope = f"from {self.since}" if self.since else "over all history"
        return f"wrote {self.rows_written} daily_pnl rows {scope}"


def compute_daily_pnl(engine: Engine, since: dt.date | None = None) -> PnlResult:
    """Recompute daily PnL, optionally only from ``since`` forward.

    Passing ``since`` is the cheap path for a daily run: recomputing a year of history
    every night works until it doesn't. Passing None recomputes everything, which is
    what you want after changing the cost basis logic.
    """
    statement = text(read_sql("daily_pnl.sql"))
    with engine.begin() as conn:
        result = conn.execute(statement, {"since": since})
        written = result.rowcount if result.rowcount is not None else 0
    return PnlResult(rows_written=written, since=since)
