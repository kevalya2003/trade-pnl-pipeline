"""Command line entry points.

``tradepnl demo`` runs the whole pipeline end to end against an empty database, which
is the fastest way for someone who is not you to see that this works.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import click
from sqlalchemy import text

from tradepnl.db import create_schema, database_url, drop_schema, make_engine
from tradepnl.generate import build_instruments, generate_trades, read_trades_csv, write_trades_csv
from tradepnl.load import load_trades
from tradepnl.models import Instrument
from tradepnl.pnl import compute_daily_pnl

DEFAULT_FEED = Path("data/trades.csv")


@click.group()
@click.option("--database-url", "db_url", default=None, help="Override TRADEPNL_DATABASE_URL.")
@click.pass_context
def main(ctx: click.Context, db_url: str | None) -> None:
    """Trade and PnL pipeline."""
    ctx.ensure_object(dict)
    ctx.obj["engine"] = make_engine(db_url)
    ctx.obj["url"] = db_url or database_url()


@main.command("init-db")
@click.pass_context
def init_db(ctx: click.Context) -> None:
    """Create tables and views, then seed the instrument dimension."""
    engine = ctx.obj["engine"]
    create_schema(engine)
    seed_instruments(engine)
    click.echo(f"schema ready at {ctx.obj['url']}")


@main.command("reset")
@click.confirmation_option(prompt="Drop every table and view?")
@click.pass_context
def reset(ctx: click.Context) -> None:
    """Drop everything. Useful when iterating on the schema."""
    drop_schema(ctx.obj["engine"])
    click.echo("schema dropped")


def seed_instruments(engine) -> int:  # noqa: ANN001 - Engine, kept loose for the CLI
    """Insert the instrument dimension if it is not already there."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    rows = [
        {
            "instrument_id": inst.instrument_id,
            "symbol": inst.symbol,
            "asset_class": inst.asset_class,
            "currency": inst.currency,
        }
        for inst in build_instruments()
    ]
    statement = pg_insert(Instrument).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[Instrument.instrument_id],
        set_={
            "symbol": statement.excluded.symbol,
            "asset_class": statement.excluded.asset_class,
            "currency": statement.excluded.currency,
        },
    )
    with engine.begin() as conn:
        conn.execute(statement)
    return len(rows)


@main.command("generate")
@click.option("--count", default=50_000, show_default=True, help="Number of clean trades.")
@click.option("--days", default=365, show_default=True, help="Days of history to spread over.")
@click.option("--seed", default=20240101, show_default=True, help="RNG seed.")
@click.option("--out", type=click.Path(path_type=Path), default=DEFAULT_FEED, show_default=True)
def generate(count: int, days: int, seed: int, out: Path) -> None:
    """Write a synthetic trade feed, including deliberate defects."""
    rows = generate_trades(build_instruments(), count=count, days=days, seed=seed)
    write_trades_csv(rows, out)
    click.echo(f"wrote {len(rows)} rows to {out} ({len(rows) - count} defective duplicates added)")


@main.command("load")
@click.option("--file", "path", type=click.Path(exists=True, path_type=Path), default=DEFAULT_FEED)
@click.option(
    "--full/--incremental",
    default=False,
    show_default=True,
    help="Full ignores the watermark and reconsiders every row.",
)
@click.pass_context
def load(ctx: click.Context, path: Path, full: bool) -> None:
    """Upsert a feed file into the trade table."""
    rows = read_trades_csv(path)
    result = load_trades(
        ctx.obj["engine"], rows, source_file=path.name, incremental=not full
    )
    click.echo(result.summary())
    click.echo(f"watermark {result.watermark_before} -> {result.watermark_after}")


@main.command("pnl")
@click.option("--since", type=click.DateTime(formats=["%Y-%m-%d"]), default=None)
@click.pass_context
def pnl(ctx: click.Context, since: dt.datetime | None) -> None:
    """Recompute the daily PnL aggregate."""
    result = compute_daily_pnl(ctx.obj["engine"], since.date() if since else None)
    click.echo(result.summary())


@main.command("stats")
@click.pass_context
def stats(ctx: click.Context) -> None:
    """Print row counts, so you can see what actually landed."""
    query = text(
        """
        SELECT
            (SELECT count(*) FROM trade)         AS trade_rows,
            (SELECT count(*) FROM v_valid_trade) AS valid_rows,
            (SELECT count(*) FROM daily_pnl)     AS daily_pnl_rows,
            (SELECT count(*) FROM instrument)    AS instruments,
            (SELECT max(watermark_at) FROM etl_watermark) AS watermark
        """
    )
    with ctx.obj["engine"].connect() as conn:
        row = conn.execute(query).mappings().one()
    invalid = row["trade_rows"] - row["valid_rows"]
    for key, value in row.items():
        click.echo(f"{key:>16}: {value}")
    click.echo(f"{'invalid_rows':>16}: {invalid}")


@main.command("demo")
@click.option("--count", default=50_000, show_default=True)
@click.pass_context
def demo(ctx: click.Context, count: int) -> None:
    """Create the schema, generate a feed, load it and aggregate it."""
    engine = ctx.obj["engine"]
    create_schema(engine)
    seeded = seed_instruments(engine)
    click.echo(f"schema ready, {seeded} instruments seeded")

    rows = generate_trades(build_instruments(), count=count)
    write_trades_csv(rows, DEFAULT_FEED)
    click.echo(f"generated {len(rows)} feed rows")

    result = load_trades(engine, rows, source_file=DEFAULT_FEED.name)
    click.echo(result.summary())

    click.echo(compute_daily_pnl(engine).summary())
    ctx.invoke(stats)


@main.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True)
@click.pass_context
def serve(ctx: click.Context, host: str, port: int) -> None:
    """Run the read-only API."""
    import uvicorn

    from tradepnl.api import create_app

    uvicorn.run(create_app(ctx.obj["engine"]), host=host, port=port)


if __name__ == "__main__":
    main()
