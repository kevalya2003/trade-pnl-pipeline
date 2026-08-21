"""Read-only HTTP surface over the PnL views.

Every endpoint is a thin wrapper over a view. That is the point: the aggregation
logic lives in SQL where the database can optimise it and a BI tool can reach it, and
the API exists only to expose it over HTTP. If an endpoint here ever grows real
business logic, it belongs in a view instead.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import Engine, text

from tradepnl.db import make_engine


def _jsonable(value: Any) -> Any:
    """Decimals lose precision silently when a JSON encoder guesses. Be explicit."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    return value


def _rows(engine: Engine, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        result = conn.execute(text(sql), params)
        return [{k: _jsonable(v) for k, v in row._mapping.items()} for row in result]


def create_app(engine: Engine | None = None) -> FastAPI:
    """Build the application. Taking the engine as an argument keeps tests honest."""
    engine = engine or make_engine()
    app = FastAPI(
        title="Trade & PnL API",
        version="0.1.0",
        description="Read-only access to trade, position and PnL aggregates.",
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness probe that actually touches the database."""
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as 503
            raise HTTPException(status_code=503, detail=f"database unreachable: {exc}") from exc
        return {"status": "ok"}

    @app.get("/instruments")
    def instruments() -> list[dict[str, Any]]:
        """The instrument dimension."""
        return _rows(
            engine,
            """
            SELECT instrument_id, symbol, asset_class, currency
            FROM instrument
            ORDER BY symbol
            """,
            {},
        )

    @app.get("/pnl/running")
    def running_pnl(
        symbol: str | None = Query(default=None, description="Filter to one symbol"),
        start: dt.date | None = Query(default=None, description="Earliest pnl_date"),
        end: dt.date | None = Query(default=None, description="Latest pnl_date"),
        limit: int = Query(default=500, ge=1, le=10_000),
    ) -> list[dict[str, Any]]:
        """Daily and cumulative realised PnL per instrument."""
        return _rows(
            engine,
            """
            SELECT *
            FROM v_running_pnl
            WHERE (CAST(:symbol AS text) IS NULL OR symbol = CAST(:symbol AS text))
              AND (CAST(:start AS date) IS NULL OR pnl_date >= CAST(:start AS date))
              AND (CAST(:end AS date) IS NULL OR pnl_date <= CAST(:end AS date))
            ORDER BY pnl_date DESC, symbol
            LIMIT :limit
            """,
            {"symbol": symbol, "start": start, "end": end, "limit": limit},
        )

    @app.get("/pnl/top")
    def top_instruments(
        month: dt.date | None = Query(default=None, description="Any date in the month"),
        top_n: int = Query(default=10, ge=1, le=100),
    ) -> list[dict[str, Any]]:
        """Instruments ranked by realised PnL within each month."""
        return _rows(
            engine,
            """
            SELECT *
            FROM v_top_instruments_by_month
            WHERE pnl_rank <= :top_n
              AND (
                  CAST(:month AS date) IS NULL
                  OR month = date_trunc('month', CAST(:month AS date))::date
              )
            ORDER BY month DESC, pnl_rank
            """,
            {"month": month, "top_n": top_n},
        )

    @app.get("/stats")
    def stats() -> dict[str, Any]:
        """Row counts and watermark, useful for confirming a load actually landed."""
        rows = _rows(
            engine,
            """
            SELECT
                (SELECT count(*) FROM trade)          AS trade_rows,
                (SELECT count(*) FROM v_valid_trade)  AS valid_trade_rows,
                (SELECT count(*) FROM daily_pnl)      AS daily_pnl_rows,
                (SELECT count(*) FROM instrument)     AS instrument_rows,
                (SELECT max(watermark_at) FROM etl_watermark) AS watermark_at
            """,
            {},
        )
        return rows[0] if rows else {}

    return app
